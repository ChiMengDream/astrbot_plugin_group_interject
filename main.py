"""群聊接话（Group Interject）

让 AstrBot 在 QQ 群里不用被 @ 也能自然地接话：

1. 重点关注的人（``focus_user_ids``，比如你自己）一说话就必定接；
2. 其他人说话时，先按 ``sample_rate`` 概率抽样，再让一个小模型判断
   「现在插一句是否自然」，通过了才接；
3. 冷却时间、每小时上限、免打扰时段，防止刷屏和烧 token；
4. 接话本身**不走自定义回复**，而是把这条消息标记成「已唤醒」，
   后面仍然由 AstrBot 原生流程生成回复 —— 所以人设、记忆、
   表情包、工具、分段回复这些能力全都照旧生效。

设计上尽量克制：宁可少接，也不要硬接。
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections import defaultdict, deque
from typing import Any

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image
from astrbot.api.star import Context, Star
from astrbot.core.message.message_event_result import MessageChain

PLUGIN_NAME = "astrbot_plugin_group_interject"

# 标记「这条消息是插件决定接话的」，供 on_llm_request 注入提示
EXTRA_KEY = "_group_interject_reason"

MAX_HISTORY = 40
MAX_REMEMBER_CHARS = 200

JUDGE_SYSTEM = "你是一个群聊插话判断器。你只输出一行 JSON，不解释、不闲聊、不用代码块。"

JUDGE_PROMPT = """你在判断：一个 AI 群友现在主动在群里插一句话，是否自然。

【这个 AI 的人设】
{self_desc}

【群里最近的聊天记录】
{history}

【最新的一条消息】
{latest}

判断标准：
1. 有人在问问题、找人搭话、聊 AI 懂或感兴趣的话题 → 应该接；
2. 明显在跟某个具体的人说话（叫了别人名字、@了别人）→ 不该接；
3. 只是语气词、表情、复读、刷屏、广告、拼多多砍一刀 → 不该接；
4. 多人快速讨论、话题很散 → 只有 AI 能补一句有价值或好笑的话时才接；
5. 宁可少接，也别硬接。硬接比不说话更烦人。

只输出一行 JSON，不要代码块：
{{"join": true 或 false, "confidence": 0 到 1 的小数, "reason": "20 字内的中文理由"}}"""

INTERJECT_HINT = """<system_reminder>
你现在在一个 QQ 群里。这条消息并没有 @ 你，是你自己决定插一句的。
要求：
- 直接说话，不要解释你为什么开口，别说「我看到你们在聊」；
- 一到两句就够，短一点，像群友顺口接一句；不要客服腔、不要列点、不要总结；
- 结合上面的群聊记录理解上下文；
- 如果实在没什么可说的，回一句很短、符合人设的反应就行。
{extra}
</system_reminder>"""


class GroupInterjectPlugin(Star):
    """群聊主动接话。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context, config)
        self.config: dict = config or {}

        # 每个群最近的消息，用于给判定模型看上下文
        self._history: dict[str, deque[dict[str, Any]]] = defaultdict(
            lambda: deque(maxlen=MAX_HISTORY)
        )
        # 时间戳类状态
        self._last_reply: dict[str, float] = {}
        self._last_judge: dict[str, float] = {}
        self._busy_until: dict[str, float] = {}
        self._reply_times: dict[str, deque[float]] = defaultdict(deque)

        # /接话 off 关掉的群
        self._group_off: set[str] = set()

        self._stats = {"judged": 0, "joined": 0}
        self._last_decision: dict[str, str] = {}

        # 主动消息状态
        self._activity: dict[str, float] = {}
        self._private_umo: dict[str, str] = {}
        self._proactive_day: dict[str, str] = {}
        self._proactive_count: dict[str, int] = {}
        self._proactive_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def initialize(self) -> None:
        try:
            off = await self.get_kv_data("group_off", [])
            if isinstance(off, list):
                self._group_off = {str(x) for x in off}
        except Exception:  # noqa: BLE001
            self.logger.warning("[群聊接话] 读取本群开关失败，使用默认值")

        self._warn_native_active_reply()
        self.logger.info(
            "[群聊接话] 已启用：重点用户 %s，冷却 %ss，判定阈值 %.2f",
            self._cfg_list("focus_user_ids", []),
            self._cfg_int("cooldown_seconds", 90),
            self._cfg_float("judge_threshold", 0.55),
        )

        private_on = self._cfg_bool("private_proactive_enable", False)
        group_on = self._cfg_bool("group_cold_enable", False)
        if private_on or group_on:
            self._proactive_task = asyncio.create_task(self._proactive_loop())
        self.logger.info(
            "[群聊接话] 私聊主动：%s（空闲 %s 分钟 / 概率 %.2f / 每天 %s 条）",
            "开" if private_on else "关",
            self._cfg_int("private_idle_minutes", 120),
            self._cfg_float("private_probability", 0.5),
            self._cfg_int("private_daily_max", 3),
        )
        self.logger.info(
            "[群聊接话] 群聊冷场搭话：%s（静默 %s 分钟 / 概率 %.2f / 每天 %s 条）",
            "开" if group_on else "关",
            self._cfg_int("group_cold_minutes", 60),
            self._cfg_float("group_cold_probability", 0.3),
            self._cfg_int("group_cold_daily_max", 2),
        )

    def _warn_native_active_reply(self) -> None:
        """自带的随机主动回复会和本插件打两枪，发现开着就提示一下。"""
        try:
            ar = (
                self.context.get_config()
                .get("provider_ltm_settings", {})
                .get("active_reply", {})
            )
            if ar.get("enable"):
                self.logger.warning(
                    "[群聊接话] 检测到 AstrBot 自带「主动回复」仍开启（概率 %s），"
                    "可能与本插件重复接话。建议到后台「对话/记忆 → 主动回复」关掉它。",
                    ar.get("possibility_reply", 0.1),
                )
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------
    def _cfg_bool(self, key: str, default: bool) -> bool:
        v = self.config.get(key, default)
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on", "是")
        return bool(v) if v is not None else default

    def _cfg_int(self, key: str, default: int) -> int:
        try:
            return int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_float(self, key: str, default: float) -> float:
        try:
            return float(self.config.get(key, default))
        except (TypeError, ValueError):
            return default

    def _cfg_str(self, key: str, default: str) -> str:
        v = self.config.get(key, default)
        return str(v) if v is not None else default

    def _cfg_list(self, key: str, default: list[str]) -> list[str]:
        v = self.config.get(key, default)
        if isinstance(v, list):
            return [str(x).strip() for x in v if str(x).strip()]
        if isinstance(v, str):
            return [s.strip() for s in re.split(r"[,，\s]+", v) if s.strip()]
        return list(default)

    # ------------------------------------------------------------------
    # 群消息入口
    # ------------------------------------------------------------------
    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE,
        priority=20,
    )
    async def on_group_message(self, event: AstrMessageEvent) -> None:
        try:
            await self._handle_group_message(event)
        except Exception:  # noqa: BLE001
            self.logger.exception("[群聊接话] 处理群消息异常")

    async def _handle_group_message(self, event: AstrMessageEvent) -> None:
        if not self._cfg_bool("enable", True):
            return

        gid = str(event.get_group_id() or "")
        if not gid:
            return

        # 记录活跃时间，供「冷场搭话」判断
        self._activity[event.unified_msg_origin] = time.time()

        text = (event.message_str or "").strip()
        components = event.get_messages()
        has_image = any(isinstance(c, Image) for c in components)
        if not text and has_image:
            text = "[图片]"
        if not text:
            return

        sender_id = str(event.get_sender_id())
        self_id = str(event.get_self_id())

        if sender_id == self_id:
            return

        is_command = text.startswith("/")
        if not is_command:
            try:
                for prefix in self.context.get_config().get("wake_prefix") or []:
                    if prefix and text.startswith(str(prefix)):
                        is_command = True
                        break
            except Exception:  # noqa: BLE001
                pass

        if not is_command:
            self._remember(gid, event.get_sender_name() or sender_id, text)

        if event.is_at_or_wake_command:
            return
        if gid in self._group_off or not self._group_allowed(gid):
            return
        if is_command:
            return
        if self._in_quiet_hours():
            self._note(gid, "免打扰时段")
            return

        focus_user = sender_id in self._cfg_list("focus_user_ids", [])
        keyword_hit = self._keyword_hit(text)

        if len(text) < self._cfg_int("min_chars", 2) and not focus_user:
            return

        if (
            self._cfg_bool("skip_when_at_others", True)
            and not focus_user
            and self._at_other(components, self_id)
        ):
            self._note(gid, "这条在 @ 别人")
            return

        now = time.time()
        if self._busy_until.get(gid, 0.0) > now:
            return

        if keyword_hit:
            reason = "触发词"
        elif focus_user and self._cfg_bool("focus_always_reply", False):
            reason = "重点用户"
        else:
            if not focus_user:
                if (
                    now - self._last_judge.get(gid, 0.0)
                    < self._cfg_int("judge_min_interval", 15)
                ):
                    return
                if random.random() > self._cfg_float("sample_rate", 0.35):
                    return

            self._last_judge[gid] = now
            ok, confidence, why = await self._judge(event, gid, text)
            self._stats["judged"] += 1
            self._note(gid, f"判定 {ok} ({confidence:.2f}) {why}")
            if not ok:
                return
            reason = f"判定通过({confidence:.2f})"

        ok, limit_reason = self._check_limits(gid)
        if not ok:
            self._note(gid, f"被限制：{limit_reason}")
            return

        self._mark_wake(event, gid, reason, text)

    def _mark_wake(
        self,
        event: AstrMessageEvent,
        gid: str,
        reason: str,
        text: str,
    ) -> None:
        event.is_at_or_wake_command = True
        event.set_extra(EXTRA_KEY, reason)

        now = time.time()
        self._last_reply[gid] = now
        self._reply_times[gid].append(now)
        self._busy_until[gid] = now + 2
        self._stats["joined"] += 1
        self._note(gid, f"接话（{reason}）")
        self.logger.info(
            "[群聊接话] %s 接话，原因=%s，消息=%s",
            gid,
            reason,
            text[:60],
        )

    # ------------------------------------------------------------------
    # 私聊入口（只记活跃时间）
    # ------------------------------------------------------------------
    @filter.event_message_type(
        filter.EventMessageType.PRIVATE_MESSAGE,
        priority=20,
    )
    async def on_private_message(self, event: AstrMessageEvent) -> None:
        try:
            umo = event.unified_msg_origin
            self._activity[umo] = time.time()
            self._private_umo[str(event.get_sender_id())] = umo
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # 请求/响应钩子
    # ------------------------------------------------------------------
    @filter.on_llm_request()
    async def inject_interject_hint(self, event: AstrMessageEvent, req) -> None:
        """接话的请求里加一段提示，让模型用「顺口插一句」的语气说话。"""
        reason = event.get_extra(EXTRA_KEY)
        if not reason:
            return
        extra = self._cfg_str("extra_instruction", "").strip()
        extra_line = f"- 额外要求：{extra}" if extra else ""
        try:
            from astrbot.core.agent.message import TextPart

            req.extra_user_content_parts.append(
                TextPart(text=INTERJECT_HINT.format(extra=extra_line)).mark_as_temp()
            )
        except Exception:  # noqa: BLE001
            self.logger.warning("[群聊接话] 注入接话提示失败")

    @filter.on_llm_response()
    async def remember_bot_reply(self, event: AstrMessageEvent, response) -> None:
        gid = str(event.get_group_id() or "")
        if not gid:
            return
        reply = (getattr(response, "completion_text", "") or "").strip()
        if reply:
            self._remember(gid, "我", reply)
            # 机器人自己说话也要刷新活跃时间，否则「冷场」判断会误判
            self._activity[event.unified_msg_origin] = time.time()

    @filter.after_message_sent()
    async def release_busy(self, event: AstrMessageEvent) -> None:
        gid = str(event.get_group_id() or "")
        if gid and event.get_extra(EXTRA_KEY):
            self._busy_until[gid] = min(
                self._busy_until.get(gid, 0.0),
                time.time() + 3,
            )

    # ------------------------------------------------------------------
    # 判定
    # ------------------------------------------------------------------
    async def _judge(
        self,
        event: AstrMessageEvent,
        gid: str,
        text: str,
        sender_name: str | None = None,
    ) -> tuple[bool, float, str]:
        provider = await self._judge_provider(event.unified_msg_origin)
        if provider is None:
            return False, 0.0, "没有可用的判定模型"

        history = list(self._history.get(gid, []))
        if history and history[-1].get("text") == text:
            history = history[:-1]
        history = history[-max(1, self._cfg_int("judge_context_size", 12)) :]
        history_text = "\n".join(f"{h['name']}: {h['text']}" for h in history)

        prompt = JUDGE_PROMPT.format(
            self_desc=self._cfg_str(
                "self_description",
                "一个叫「大肥鱼」的 AI 群友：爱吃白饭、有点懒、嘴贫，说话短，爱接梗。",
            ),
            history=history_text or "（暂无）",
            latest=(
                f"{sender_name or event.get_sender_name()}: {text}"
                + (
                    "\n（这条来自你比较熟的人，可以比平时更积极一点）"
                    if str(event.get_sender_id())
                    in self._cfg_list("focus_user_ids", [])
                    else ""
                )
            ),
        )

        try:
            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    session_id=f"gj-{gid}-{int(time.time() * 1000)}",
                    system_prompt=JUDGE_SYSTEM,
                ),
                timeout=self._cfg_int("judge_timeout", 20),
            )
        except asyncio.TimeoutError:
            return False, 0.0, "判定超时"
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[群聊接话] 判定请求失败: %s", exc)
            return False, 0.0, "判定请求失败"

        raw = (getattr(response, "completion_text", "") or "").strip()
        data = self._parse_json(raw)
        if not isinstance(data, dict):
            return False, 0.0, f"判定结果无法解析：{raw[:30]}"

        join = bool(data.get("join"))
        try:
            confidence = float(data.get("confidence", 0))
        except (TypeError, ValueError):
            confidence = 0.0
        reason = str(data.get("reason", ""))[:40]
        is_focus_sender = str(event.get_sender_id()) in self._cfg_list(
            "focus_user_ids", []
        )
        threshold = (
            self._cfg_float("focus_threshold", 0.35)
            if is_focus_sender
            else self._cfg_float("judge_threshold", 0.55)
        )
        ok = join and confidence >= threshold
        return ok, confidence, reason

    async def _judge_provider(self, umo: str):
        provider_id = self._cfg_str("judge_provider_id", "").strip()
        if provider_id:
            provider = self.context.get_provider_by_id(provider_id)
            if provider is not None:
                return provider
            self.logger.warning(
                "[群聊接话] 找不到判定模型 %s，改用默认模型", provider_id
            )
        return await self.context.get_using_provider_async(umo)

    @staticmethod
    def _parse_json(raw: str) -> Any:
        text = raw.strip()
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None

    # ------------------------------------------------------------------
    # 各种闸门
    # ------------------------------------------------------------------
    def _group_allowed(self, gid: str) -> bool:
        blacklist = self._cfg_list("group_blacklist", [])
        if gid in blacklist:
            return False
        whitelist = self._cfg_list("group_whitelist", [])
        return not (whitelist and gid not in whitelist)

    def _keyword_hit(self, text: str) -> bool:
        return any(k and k in text for k in self._cfg_list("keyword_triggers", []))

    def _at_other(self, components, self_id: str) -> bool:
        for comp in components:
            if isinstance(comp, At):
                qq = str(comp.qq)
                if qq not in (self_id, "all"):
                    return True
        return False

    def _check_limits(self, gid: str) -> tuple[bool, str]:
        now = time.time()
        cooldown = self._cfg_int("cooldown_seconds", 90)
        if cooldown > 0 and now - self._last_reply.get(gid, 0.0) < cooldown:
            return False, f"冷却中（{cooldown}s）"

        cap = self._cfg_int("max_per_hour", 20)
        if cap > 0:
            times = self._reply_times[gid]
            while times and now - times[0] > 3600:
                times.popleft()
            if len([t for t in times if now - t <= 3600]) >= cap:
                return False, f"本小时已达上限（{cap}）"
        return True, ""

    def _in_quiet_hours(self) -> bool:
        if not self._cfg_bool("quiet_hours_enable", False):
            return False
        start = self._parse_hhmm(self._cfg_str("quiet_start", "01:00"))
        end = self._parse_hhmm(self._cfg_str("quiet_end", "07:00"))
        if start is None or end is None or start == end:
            return False
        now = time.localtime()
        minutes = now.tm_hour * 60 + now.tm_min
        if start < end:
            return start <= minutes < end
        return minutes >= start or minutes < end

    @staticmethod
    def _parse_hhmm(value: str) -> int | None:
        match = re.match(r"^\s*(\d{1,2})\s*[:：]\s*(\d{1,2})\s*$", value or "")
        if not match:
            return None
        hour, minute = int(match.group(1)), int(match.group(2))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            return None
        return hour * 60 + minute

    # ------------------------------------------------------------------
    # 上下文
    # ------------------------------------------------------------------
    def _remember(self, gid: str, name: str, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        if len(text) > MAX_REMEMBER_CHARS:
            text = text[:MAX_REMEMBER_CHARS] + "…"
        self._history[gid].append(
            {"name": name or "未知", "text": text, "ts": time.time()}
        )

    def _note(self, gid: str, reason: str) -> None:
        self._last_decision[gid] = reason
        if self._cfg_bool("debug_log", True):
            self.logger.info("[群聊接话] %s %s", gid, reason)

    # ------------------------------------------------------------------
    # 主动说话：私聊来找你 / 群聊冷场搭话
    # ------------------------------------------------------------------
    PRIVATE_PROMPT = (
        '你想主动找老大说话。要求：\n1. 一到两句，口语，像真人发消息；\n2. 语气要毒：可以嫌弃、吐槽、阴阳怪气，带点「你居然还知道理我」的意思，但别真骂脏话；\n3. 必须给个具体由头或问句，比如「在干嘛」「有空没」「饭吃了没」，不要只发「在吗」；\n4. 禁止客服腔：不说「有什么可以帮您」「作为AI」，不解释你为什么发消息；\n5. 有最近的对话就顺着说，不要重新打招呼。'
    )
    GROUP_PROMPT = (
        '群里冷场很久了，你随口起个话头。要求：\n1. 一到两句，像群友随手发；\n2. 语气带刺，可以吐槽、阴阳怪气，别客客气气；\n3. 别问「有人吗」「怎么都不说话」，别像主持人；\n4. 禁止客服腔。'
    )

    async def _proactive_loop(self) -> None:
        interval = max(30, self._cfg_int("private_check_seconds", 300))
        while True:
            try:
                await asyncio.sleep(interval)
                await self._tick_private()
                await self._tick_group()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("[群聊接话] 主动消息循环出错: %s", exc)

    async def _recent_context(self, umo: str, limit: int = 8) -> str:
        """取最近几轮对话，供主动消息接话。取不到就返回空串。"""
        try:
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return ""
            lines, _ = await cm.get_human_readable_context(
                umo, cid, page=1, page_size=limit
            )
            return "\n".join(str(x) for x in lines[-limit:])
        except Exception:  # noqa: BLE001
            return ""

    async def _remember_sent(self, umo: str, text: str) -> None:
        """把主动发出去的话写进对话历史，这样之后她记得自己说过。"""
        try:
            cm = self.context.conversation_manager
            cid = await cm.get_curr_conversation_id(umo)
            if not cid:
                return
            conv = await cm.get_conversation(umo, cid)
            raw = getattr(conv, "history", "") if conv else ""
            history = json.loads(raw) if raw else []
            if not isinstance(history, list):
                return
            history.append({"role": "assistant", "content": text})
            await cm.update_conversation(umo, cid, history=history)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[群聊接话] 写回主动消息失败: %s", exc)

    async def _compose(self, umo: str, prompt: str, persona: str = "") -> str:
        """让模型生成一句话；带上最近对话，失败返回空串。"""
        provider = await self._judge_provider(umo)
        if provider is None:
            return ""

        recent = await self._recent_context(umo)
        full_prompt = prompt
        if recent:
            full_prompt = (
                f"{prompt}\n\n"
                f"【你们最近的对话，供你接话，不要照抄】\n{recent}\n\n"
                "如果上面有内容，就顺着它说；不要像第一次聊天那样重新打招呼。"
            )

        try:
            resp = await asyncio.wait_for(
                provider.text_chat(
                    prompt=full_prompt,
                    system_prompt=persona or None,
                    session_id=f"pro-{int(time.time() * 1000)}",
                ),
                timeout=self._cfg_int("private_timeout", 30),
            )
        except asyncio.TimeoutError:
            self.logger.warning("[群聊接话] 主动消息生成超时")
            return ""
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("[群聊接话] 主动消息生成失败: %s", exc)
            return ""
        return (getattr(resp, "completion_text", "") or "").strip()[:300]

    # ---------------------------- 私聊：来找你 ----------------------------
    def _private_targets(self) -> list[str]:
        explicit = self._cfg_list("private_targets", [])
        candidates = list(explicit)
        if not candidates:
            for qq in self._cfg_list("focus_user_ids", []):
                umo = self._private_umo.get(qq)
                if umo:
                    candidates.append(umo)
        return [u for u in candidates if "GroupMessage" not in u]

    def _private_target(self, event: AstrMessageEvent) -> str:
        """定位私聊 umo：当前是私聊就用它，否则用记录过的，最后按平台拼。"""
        sender = str(event.get_sender_id())
        umo = event.unified_msg_origin or ""
        if "GroupMessage" not in umo:
            return umo
        recorded = self._private_umo.get(sender)
        if recorded:
            return recorded
        return f"{event.get_platform_id()}:FriendMessage:{sender}"

    async def _tick_private(self) -> None:
        if not self._cfg_bool("private_proactive_enable", False):
            return
        if self._in_quiet_hours():
            return

        now = time.time()
        idle = self._cfg_int("private_idle_minutes", 120) * 60
        daily = self._cfg_int("private_daily_max", 3)
        today = time.strftime("%Y-%m-%d")

        for umo in self._private_targets():
            last = self._activity.get(umo)
            if not last:
                self._activity[umo] = now
                continue
            if now - last < idle:
                continue
            key = "p:" + umo
            if self._proactive_day.get(key) != today:
                self._proactive_day[key] = today
                self._proactive_count[key] = 0
            if self._proactive_count.get(key, 0) >= daily:
                continue
            if random.random() > self._cfg_float("private_probability", 0.5):
                continue

            text = await self._compose(
                umo,
                self._cfg_str("private_prompt", self.PRIVATE_PROMPT),
                self._cfg_str("private_persona", ""),
            )
            if not text:
                continue
            try:
                await self.context.send_message(umo, MessageChain().message(text))
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("[群聊接话] 私聊主动发送失败: %s", exc)
                continue

            await self._remember_sent(umo, text)
            self._activity[umo] = time.time()
            self._proactive_count[key] = self._proactive_count.get(key, 0) + 1
            self._stats["joined"] += 1
            self.logger.info(
                "[群聊接话] 私聊主动 %s (%s/%s): %s",
                umo, self._proactive_count[key], daily, text[:50],
            )

    # ---------------------------- 群聊：冷场搭话 ----------------------------
    def _group_targets(self) -> list[str]:
        allowed = self._cfg_list("group_cold_groups", [])
        targets = []
        for umo in list(self._activity.keys()):
            if "GroupMessage" not in umo:
                continue
            gid = umo.rsplit(":", 1)[-1]
            if allowed and gid not in allowed:
                continue
            if gid in self._group_off or not self._group_allowed(gid):
                continue
            targets.append(umo)
        return targets

    async def _tick_group(self) -> None:
        if not self._cfg_bool("group_cold_enable", False):
            return
        if self._in_quiet_hours():
            return

        now = time.time()
        idle = self._cfg_int("group_cold_minutes", 60) * 60
        daily = self._cfg_int("group_cold_daily_max", 2)
        today = time.strftime("%Y-%m-%d")

        for umo in self._group_targets():
            last = self._activity.get(umo)
            if not last:
                continue
            if now - last < idle:
                continue
            key = "g:" + umo
            if self._proactive_day.get(key) != today:
                self._proactive_day[key] = today
                self._proactive_count[key] = 0
            if self._proactive_count.get(key, 0) >= daily:
                continue
            if random.random() > self._cfg_float("group_cold_probability", 0.3):
                continue

            text = await self._compose(
                umo,
                self._cfg_str("group_cold_prompt", self.GROUP_PROMPT),
                self._cfg_str("group_cold_persona", ""),
            )
            if not text:
                continue
            try:
                await self.context.send_message(umo, MessageChain().message(text))
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("[群聊接话] 群聊主动发送失败: %s", exc)
                continue

            await self._remember_sent(umo, text)
            self._activity[umo] = time.time()
            self._proactive_count[key] = self._proactive_count.get(key, 0) + 1
            self._stats["joined"] += 1
            self.logger.info(
                "[群聊接话] 群聊冷场搭话 %s (%s/%s): %s",
                umo, self._proactive_count[key], daily, text[:50],
            )

    # ------------------------------------------------------------------
    # 指令：/接话 [状态|on|off|主动|测试|清空]
    # ------------------------------------------------------------------
    @filter.command("接话")
    async def cmd_interject(self, event: AstrMessageEvent):
        """查看状态；群聊里还能开关本群、试判定；私聊/群聊都能手动触发主动消息"""
        gid = str(event.get_group_id() or "")
        raw = (event.message_str or "").strip()
        parts = raw.split()
        sub = parts[1].lower() if len(parts) > 1 else ""
        in_group = bool(gid)

        if in_group and sub in ("on", "off", "开", "关"):
            if not self._can_manage(event):
                yield event.plain_result("只有重点用户或管理员能开关本群接话。")
                return
            turn_on = sub in ("on", "开")
            if turn_on:
                self._group_off.discard(gid)
            else:
                self._group_off.add(gid)
            await self._save_group_switch()
            yield event.plain_result("本群接话已开启。" if turn_on else "本群接话已关闭。")
            return

        if sub in ("主动", "proactive"):
            if in_group:
                text = await self._compose(
                    event.unified_msg_origin,
                    self._cfg_str("group_cold_prompt", self.GROUP_PROMPT),
                    self._cfg_str("group_cold_persona", ""),
                )
                if not text:
                    yield event.plain_result("生成失败，看看后台日志。")
                    return
                try:
                    await self.context.send_message(
                        event.unified_msg_origin, MessageChain().message(text)
                    )
                except Exception as exc:  # noqa: BLE001
                    yield event.plain_result(f"发送失败：{exc}")
                    return
                await self._remember_sent(event.unified_msg_origin, text)
                yield event.plain_result("已在群里主动说了一句：" + text)
            else:
                target = self._private_target(event)
                text = await self._compose(
                    target,
                    self._cfg_str("private_prompt", self.PRIVATE_PROMPT),
                    self._cfg_str("private_persona", ""),
                )
                if not text:
                    yield event.plain_result("生成失败，看看后台日志。")
                    return
                try:
                    await self.context.send_message(target, MessageChain().message(text))
                except Exception as exc:  # noqa: BLE001
                    yield event.plain_result(f"发送失败：{exc}")
                    return
                await self._remember_sent(target, text)
                yield event.plain_result("已主动发了一条：" + text)
            return

        if in_group and sub in ("测试", "test"):
            yield event.plain_result(await self._test_judge(event, gid))
            return

        if in_group and sub in ("清空", "reset"):
            self._history[gid].clear()
            yield event.plain_result("本群上下文已清空。")
            return

        yield event.plain_result(self._status_text(gid))

    async def _test_judge(self, event: AstrMessageEvent, gid: str) -> str:
        history = self._history.get(gid)
        if not history:
            return "本群还没有记录到消息，先在群里说两句再试。"
        last = history[-1]
        ok, confidence, why = await self._judge(
            event, gid, last["text"], sender_name=last["name"]
        )
        return (
            f"测试对象：{last['name']}: {last['text'][:60]}\n"
            f"判定结果：{'接' if ok else '不接'}\n"
            f"把握度：{confidence:.2f}（阈值 {self._cfg_float('judge_threshold', 0.55):.2f}）\n"
            f"理由：{why or '（无）'}"
        )

    def _status_text(self, gid: str) -> str:
        private_on = self._cfg_bool("private_proactive_enable", False)
        group_on = self._cfg_bool("group_cold_enable", False)
        private_line = (
            f"私聊来找你：{'开' if private_on else '关'}"
            f"（空闲 {self._cfg_int('private_idle_minutes', 120)} 分钟 /"
            f" 概率 {self._cfg_float('private_probability', 0.5):.2f} /"
            f" 每天 {self._cfg_int('private_daily_max', 3)} 条）"
        )
        group_line = (
            f"群聊冷场搭话：{'开' if group_on else '关'}"
            f"（静默 {self._cfg_int('group_cold_minutes', 60)} 分钟 /"
            f" 概率 {self._cfg_float('group_cold_probability', 0.3):.2f} /"
            f" 每天 {self._cfg_int('group_cold_daily_max', 2)} 条）"
        )

        if not gid:
            return private_line + "\n" + group_line + "\n（在群里发 /接话 看群设置）"

        enabled = gid not in self._group_off and self._group_allowed(gid)
        last = self._last_reply.get(gid)
        last_text = (
            time.strftime("%H:%M:%S", time.localtime(last)) if last else "还没接过话"
        )
        return (
            f"群聊接话：{'开' if enabled else '关'}\n"
            f"冷却：{self._cfg_int('cooldown_seconds', 90)} 秒 /"
            f" 每小时上限 {self._cfg_int('max_per_hour', 20)} 次\n"
            f"抽样比例：{self._cfg_float('sample_rate', 0.35):.2f}"
            f" / 阈值 {self._cfg_float('judge_threshold', 0.55):.2f}\n"
            f"上次接话：{last_text}\n"
            f"最近一次判定：{self._last_decision.get(gid, '（无）')}\n"
            f"—— 主动消息 ——\n"
            f"{private_line}\n"
            f"{group_line}"
        )

    def _can_manage(self, event: AstrMessageEvent) -> bool:
        if getattr(event, "role", "member") == "admin":
            return True
        return str(event.get_sender_id()) in self._cfg_list("focus_user_ids", [])

    async def _save_group_switch(self) -> None:
        try:
            await self.put_kv_data("group_off", sorted(self._group_off))
        except Exception:  # noqa: BLE001
            self.logger.warning("[群聊接话] 保存本群开关失败")

    async def terminate(self) -> None:
        if self._proactive_task is not None:
            self._proactive_task.cancel()
            self._proactive_task = None
        await self._save_group_switch()
