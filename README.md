# 群聊接话（astrbot_plugin_group_interject）

让 AstrBot 在 QQ 群里**不用被 @ 也能自然接话**。

- **你说的话优先判定**：`focus_user_ids` 里的 QQ 号，每句话都会送判定（不走概率抽样），
  再用更低的 `focus_threshold` 决定接不接 —— 也就是「选择性回你的话」
- **别人说话偶尔接**：先按比例抽样，再让小模型判断「现在插一句自然吗」，通过才接
- **叫它名字就接**：`keyword_triggers` 命中（如「小鱼」「肥鱼」）直接接
- **不刷屏**：同群冷却时间、每小时上限、免打扰时段
- **不打断原流程**：接话时只把消息标记成「已唤醒」，回复仍由 AstrBot 原生 Agent 生成，
  人设、长期记忆、表情包、工具、分段回复全部照旧生效

## 安装

把本目录放到：

```
~/.astrbot/data/plugins/astrbot_plugin_group_interject/
```

（Windows 即 `C:\Users\<你>\.astrbot\data\plugins\`）

然后**重启 AstrBot**（或后台「插件」页点重载）。加载成功后日志会打印：

```
[群聊接话] 已启用：重点用户 ['1802881876']，冷却 90s，判定阈值 0.55
```

## 使用

| 操作 | 说明 |
| --- | --- |
| `/接话` | 查看本群状态、上次接话时间、最近一次判定理由、统计 |
| `/接话 on` / `/接话 off` | 开关本群接话（重点用户或管理员可用，重启后保留） |
| `/接话 测试` | 对最近一条消息试判定，只告诉你「接/不接 + 理由」，不会真的说话 |
| `/接话 清空` | 清空本群上下文 |

调参建议：先看 `/接话 测试` 的理由，觉得太安静就调高 `sample_rate`、调低
`judge_threshold`；觉得吵就反过来，或加大 `cooldown_seconds`。

## 配置项（后台 → 插件 → 群聊接话）

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `enable` | 开 | 总开关 |
| `focus_user_ids` | 你的 QQ | 重点用户：每句都判定、阈值更低 |
| `focus_always_reply` | 关 | 开 = 重点用户一说话就回，完全不看判定 |
| `focus_threshold` | 0.35 | 重点用户的判定阈值，越低越容易接你的话 |
| `keyword_triggers` | 小鱼/肥鱼/大肥鱼/吃白饭 | 说到就接 |
| `group_whitelist` / `group_blacklist` | 空 | 留空=所有群生效 |
| `cooldown_seconds` | 90 | 同群两次接话最短间隔（0 = 不限） |
| `max_per_hour` | 20 | 同群每小时上限 |
| `sample_rate` | 0.35 | 非重点消息送去判定的比例 |
| `judge_threshold` | 0.55 | 判定通过阈值，调高更沉默 |
| `judge_provider_id` | 空 | 判定用模型，建议填便宜的，如 `deepseek/deepseek-flash` |
| `judge_context_size` | 12 | 判定时参考多少条群消息 |
| `judge_min_interval` | 15 | 同群两次判定最短间隔（秒） |
| `min_chars` | 2 | 太短的消息不接 |
| `skip_when_at_others` | 开 | 消息 @ 了别人就不插嘴 |
| `quiet_hours_enable` / `quiet_start` / `quiet_end` | 关 / 01:00 / 07:00 | 免打扰时段，支持跨零点 |
| `self_description` | 大肥鱼人设 | 给判定模型看的 Bot 人设 |
| `extra_instruction` | 空 | 接话时的额外要求 |
| `debug_log` | 开 | 打印每次判定结果，调完可关 |

## 注意

AstrBot 自带「主动回复」（`provider_ltm_settings.active_reply`，随机 10% 接话）
会和本插件重复接话。安装时已尝试把它关掉；如果后台又打开了，插件启动日志会警告，
请手动到「对话/记忆 → 主动回复」关掉。

## 实现要点（给改代码的人）

- 群消息默认走 `waking_check` 阶段，只有 @、@全体、引用 Bot、唤醒词、或插件 handler
  命中才会 `is_wake`；`is_at_or_wake_command` 才是真正触发 LLM 回复的开关。
- 本插件用 `@filter.event_message_type(GROUP_MESSAGE, priority=20)` 拿到**所有**群消息
  （顺带把它们记进上下文），决定接话时把 `event.is_at_or_wake_command` 置 `True`，
  之后 `ProcessStage` 会像被 @ 一样调用原生 Agent。
- 用 `@filter.on_llm_request()` 往请求里塞一段 system_reminder，说明「这条没 @ 你，
  是你自己插话」，避免模型用客服腔或反问「找我什么事」。
- 判定用 `provider.text_chat()` 单独发一次轻量请求，不进入对话历史。
