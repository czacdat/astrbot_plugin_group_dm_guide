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

import asyncio
import re
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


# --------------------------------------------------------------------------- #
# 提问识别
#
# 强信号（默认算提问）：
#   · 句尾问号 ？ ?
#   · 「吗」结尾
#   · 明确求助词：请问 / 请教 / 求问 / 求助 / 有人知道 / 谁知道 / 帮我 …
# 弱信号（默认不算，可选开启）：
#   · 只出现疑问词：什么 / 怎么 / 为什么 / 哪里 / 多少 / 是不是 / 有没有 …
#
# 误判防护（用真实群聊记录校准过）：
#   · 先剥掉 [引用消息(...)] —— 引用里的问号不是当前人的提问
#   · 剥掉 [At:xxx]、@昵称、（动作）、[图片] 等标记
#   · 单独一个「？」「？？」是惊讶，不算提问
#   · 「？！」「！？」组合是感叹，不算提问
# --------------------------------------------------------------------------- #

STRONG_REQUEST_WORDS = (
    "请问", "请教", "求问", "问一下", "问下", "想问",
    "求助", "有人知道", "有人会", "谁知道", "谁会", "有大佬",
    "帮我", "帮忙", "教教我", "教一下",
)

WEAK_QUESTION_WORDS = (
    "什么", "怎么", "怎样", "为什么", "为啥", "如何",
    "哪里", "哪个", "哪儿", "哪位", "在哪",
    "多少", "几个", "多久", "多大", "多长",
    "是否", "能否", "能不能", "可不可以", "是不是", "有没有",
    "会不会", "要不要", "行不行", "对不对", "好不好",
    "咋办", "咋样", "怎么办", "咋整", "咋弄",
    "是什么", "有什么",
)

# 感叹/吐槽信号：出现时不把弱信号当提问
EXCLAMATION_WORDS = (
    "哈哈", "笑死", "草", "绝了", "好家伙", "绷不住", "离谱", "服了",
    "泪目", "逆天", "卧槽", "我哭", "乐死", "太", "这么", "那么", "真",
)

# 反问信号：不是在求答案
RHETORICAL_MARKERS = ("怎么可能", "怎么会", "难道", "不至于", "哪有")

GREETINGS = frozenset(
    {
        "在吗", "在不在", "在么", "你好", "您好", "hi", "hello", "嗨",
        "谢谢", "谢了", "多谢", "感谢", "好的", "好", "行", "可以",
        "嗯", "嗯嗯", "哦", "哦哦", "收到", "了解", "明白", "知道了",
        "ok", "okay", "早", "早上好", "晚安", "拜拜", "再见",
        "对", "是", "不是", "没有", "有",
    }
)

_QUOTE_RE = re.compile(r"\[引用消息.*?\)\]", re.DOTALL)
_AT_TAG_RE = re.compile(r"\[At:[^\]]*\]")
_MEDIA_TAG_RE = re.compile(r"\[(?:图片|表情|语音|视频|文件|ComponentType[^\]]*)\]")
_MENTION_RE = re.compile(r"@\S+")
_ACTION_RE = re.compile(r"[（(][^）)]{0,40}[）)]")
_EMOJI_ONLY_RE = re.compile(r"^[\s\W_]+$", re.UNICODE)
_PUNCT_RE = re.compile(r"[，,。.！!、~～\s]+")


def strip_decorations(text: str) -> str:
    """剥掉引用、@、图片标记、动作描写，留下当前人真正说的话。"""
    s = str(text or "")
    s = _QUOTE_RE.sub("", s)      # 引用消息整段（里面可能有问号）
    s = _AT_TAG_RE.sub("", s)     # [At:123]
    s = _MEDIA_TAG_RE.sub("", s)  # [图片] 等
    s = _MENTION_RE.sub("", s)    # @昵称
    s = _ACTION_RE.sub("", s)     # （动作）
    return s.strip()


def count_spoken_chars(text: str) -> int:
    """统计「真正说出口」的字数。

    不算 @ 昵称、引用内容、图片标记、动作描写——被 @ 的人名不该算进回答字数。
    """
    return len(_PUNCT_RE.sub("", strip_decorations(text)))


def detect_question(text: str, extra_words: tuple[str, ...] = ()) -> tuple[bool, str]:
    """强信号提问判断。返回 (是否提问, 理由)。"""
    raw = str(text or "").strip()
    if not raw:
        return False, "empty"

    s = strip_decorations(raw)
    if not s:
        return False, "only_mention_or_action"
    if _EMOJI_ONLY_RE.match(s):
        return False, "emoji_only"

    core = _PUNCT_RE.sub("", s).lower()
    if not core:
        return False, "punct_only"
    if core in GREETINGS:
        return False, "greeting"
    if any(w in s for w in RHETORICAL_MARKERS):
        return False, "rhetorical"

    # 强信号 1：句尾问号（必须句尾，且整句至少 3 字）
    if s.endswith(("？！", "！？", "?!", "!?")):
        return False, "exclam_question"
    if s.endswith(("？", "?")):
        return (True, "question_mark") if len(core) >= 3 else (False, "bare_question_mark")

    # 强信号 2：「吗」结尾
    if core.endswith("吗"):
        return True, "ends_with_ma"

    # 强信号 3：明确求助词（含用户自定义词）
    for w in tuple(STRONG_REQUEST_WORDS) + tuple(extra_words):
        if w and w in s:
            return True, "request:" + w

    return False, "none"


def detect_weak_question(
    text: str, extra_words: tuple[str, ...] = ()
) -> tuple[bool, str]:
    """弱信号提问判断（仅在开启「疑问词也算」时使用）。"""
    ok, reason = detect_question(text, extra_words)
    if ok:
        return True, reason

    s = strip_decorations(text)
    if not s:
        return False, "no_text"

    core = _PUNCT_RE.sub("", s).lower()
    if len(core) <= 2:
        return False, "too_short"
    if any(w in s for w in RHETORICAL_MARKERS):
        return False, "rhetorical"
    if any(w in s for w in EXCLAMATION_WORDS):
        return False, "exclamation"

    hits = [w for w in WEAK_QUESTION_WORDS if w in s]
    if hits:
        return True, "weak:" + hits[0]
    return False, "none"


@register(
    "astrbot_plugin_group_dm_guide",
    "DSH",
    "群聊高频提问引导：同一人在群里问太多次后，把回答私聊发给他；可选冷却期不响应",
    "1.5.0",
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
        # 群里提示语的限频：同一个人在这个时间内只提示一次（0 = 不限）
        self.notice_cooldown_minutes = max(
            0, int(cfg.get("notice_cooldown_minutes") or 0)
        )
        # 私聊失败后是否隔一会儿再试一次（应对临时网络抖动）
        self.retry_private_once = bool(cfg.get("retry_private_once", True))

        # 只统计「提问」：日常闲聊不计数、也不转私聊
        self.only_questions = bool(cfg.get("only_questions", True))
        # 疑问词（什么/怎么/为什么…）单独出现是否也算提问。默认不算，误报高
        self.count_weak_questions = bool(cfg.get("count_weak_questions", False))
        # 用户自定义的额外提问关键词（命中即算提问）
        self.extra_question_words = tuple(
            str(x).strip() for x in (cfg.get("extra_question_words") or []) if str(x).strip()
        )

        self.whitelist = {str(x).strip() for x in (cfg.get("whitelist") or []) if str(x).strip()}
        self.group_whitelist = {
            str(x).strip() for x in (cfg.get("group_whitelist") or []) if str(x).strip()
        }

        self._hits: dict[str, list[float]] = {}
        self._cooldown: dict[str, float] = {}
        # 提示语限频：key -> 上次提示时间
        self._notice_at: dict[str, float] = {}
        self._last_prune = time.time()

        logger.info(
            "[群聊引导] 已加载: 模式=%s, 阈值=%s次/%s分钟, 冷却=%s分钟, 最短转私聊=%s字, "
            "私聊失败拦截=%s, 提示限频=%s分钟, dry_run=%s, "
            "只统计提问=%s(弱信号=%s, 自定义词=%s个)",
            "转私聊" if self.redirect_reply else "发引导语",
            self.trigger_count,
            self.window_minutes,
            self.cooldown_minutes,
            self.min_redirect_chars,
            self.intercept_on_fail,
            self.notice_cooldown_minutes,
            self.dry_run,
            self.only_questions,
            self.count_weak_questions,
            len(self.extra_question_words),
        )

    # ------------------------------------------------------------------ #
    # 工具方法
    # ------------------------------------------------------------------ #
    @staticmethod
    def _key(group_id: str, user_id: str) -> str:
        return f"{group_id}|{user_id}"

    def _is_question(self, event: AstrMessageEvent) -> tuple[bool, str]:
        """判断本轮消息是不是提问。返回 (是否提问, 理由)。"""
        try:
            text = str(getattr(event, "message_str", "") or "")
        except Exception:  # noqa: BLE001
            return False, "no_text"

        if self.count_weak_questions:
            return detect_weak_question(text, self.extra_question_words)
        return detect_question(text, self.extra_question_words)


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
        # 提示限频记录只保留 1 小时内的
        for key, at in list(self._notice_at.items()):
            if now - at > 3600:
                self._notice_at.pop(key, None)

    def _notice_allowed(self, key: str, now: float) -> bool:
        """群里提示语的限频：同一 (群, 人) 在窗口内只提示一次。

        否则对方在冷却期连问 5 次，群里就会刷 5 条「私聊说吧」——比不提示还吵。
        """
        if self.notice_cooldown_minutes <= 0:
            return True
        last = self._notice_at.get(key, 0.0)
        if now - last < self.notice_cooldown_minutes * 60:
            return False
        self._notice_at[key] = now
        return True

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

            rounds = 2 if self.retry_private_once else 1
            for round_idx in range(rounds):
                if round_idx:
                    # 临时网络抖动很常见，隔一下再试一次
                    await asyncio.sleep(1.5)
                    logger.info("[群聊引导] 私聊没发出去，1.5 秒后重试一次: user=%s", user_id)
                for params, channel in attempts:
                    try:
                        await bot.send_private_msg(**params)
                        return channel
                    except Exception as exc:  # noqa: BLE001
                        logger.debug(
                            "[群聊引导] 私聊尝试失败(round=%s channel=%s group_id=%s): %s",
                            round_idx + 1,
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

        # 只统计提问：日常闲聊直接放行（不计数、不转私聊、冷却期也不拦）
        if self.only_questions:
            is_q, q_reason = self._is_question(event)
            if not is_q:
                logger.debug(
                    "[群聊引导] 非提问，放行: group=%s user=%s reason=%s",
                    group_id, user_id, q_reason,
                )
                return

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

        # 字数只算「真正说出口」的部分：@昵称、引用内容、动作描写都不算
        spoken_chars = count_spoken_chars(text)

        # 回答太短就不折腾了，正常留在群里
        if self.min_redirect_chars and spoken_chars < self.min_redirect_chars:
            logger.info(
                "[群聊引导] 回答有效字数 %s（原文 %s，< %s），不转私聊: user=%s",
                spoken_chars,
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
            logger.info("[群聊引导] dry_run：本应私聊发送 %s 字（有效 %s）给 %s", len(text), spoken_chars, user_id)
            return

        try:
            group_id = str(event.get_group_id() or "").strip()
        except Exception:  # noqa: BLE001
            group_id = ""

        channel = await self._send_private(event, user_id, group_id, text)
        # 提示语限频：同一个人短时间内只提示一次，避免反过来刷屏
        notice_key = self._key(group_id, user_id)
        can_notice = self._notice_allowed(notice_key, time.time())

        if channel:
            # 清空正文，群里不再重复这条回答
            response.completion_text = ""
            if self.group_notice and can_notice:
                try:
                    await event.send(event.plain_result(self.group_notice))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[群聊引导] 群里提示发送失败: %s", exc)
            elif self.group_notice:
                logger.info("[群聊引导] 提示限频中，群里不再重复提示: user=%s", user_id)
            logger.info(
                "[群聊引导] 已转私聊(%s): user=%s 字数=%s（有效 %s）",
                "好友" if channel == "friend" else "临时会话",
                user_id,
                len(text),
                spoken_chars,
            )
            return

        if self.intercept_on_fail:
            # 私聊发不出去：仍然拦截，只留一句提示
            response.completion_text = ""
            notice = self.blocked_notice if can_notice else ""
            logger.info(
                "[群聊引导] 私聊未送达，已拦截群回复: user=%s 字数=%s（有效 %s）提示=%s",
                user_id,
                len(text),
                spoken_chars,
                "发" if notice else "限频跳过",
            )
        else:
            # 退回群里正常回复
            notice = self.fallback_notice if can_notice else ""
            logger.info("[群聊引导] 私聊未送达，退回群里回复: user=%s", user_id)

        if notice:
            try:
                await event.send(event.plain_result(notice))
            except Exception as exc:  # noqa: BLE001
                logger.warning("[群聊引导] 提示发送失败: %s", exc)


