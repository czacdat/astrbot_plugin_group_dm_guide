# 群聊高频提问引导（astrbot_plugin_group_dm_guide）

> 同一个群友问太多次？把回答挪到私聊去，群聊留给别人。

[![AstrBot](https://img.shields.io/badge/AstrBot-%3E%3D4.24.0-blue)](https://github.com/AstrBotDevs/AstrBot)

---

## 这个插件做什么

群里总有人高频问问题，一条接一条，群聊被刷满。

这个插件会在**同一个人**于时间窗内问太多次之后，把她的回答**私聊发给这个人**，
群里只留一句提示。之后一段时间内他再问，回答都走私聊。

```
同一人 10 分钟内问了第 3 次
        ↓
模型照常生成回答
        ↓
回答  ──→ 私聊发给提问的人
群里  ──→ 只留一句「（这里不方便说，私聊发你了）」
```

**不是不回，是换个地方回。**

## 特性

- **回答转私聊**：不走群聊刷屏，直接私聊送达
- **三层兜底**：好友私聊 → 群临时会话 → 拦截群回复只留提示
- **字数门槛**：太短的回答（"嗯。"）不折腾，照常留在群里
- **失败可拦截**：私聊发不出去时，可以选择拦掉群回复，只留一句提示
- **可试运行**：`dry_run` 只记日志、不改发送行为，先观察判断准不准
- **精细豁免**：管理员 / 指定用户 / 只对指定群生效
- **零依赖**：只用 AstrBot 自带 API，不需要额外安装包

## 安装

### 方式一：面板上传（推荐）

1. 把仓库打包成 zip（或直接下载 Release 里的 zip）
2. 面板 → 插件管理 → 安装插件 → 上传本地文件

### 方式二：手动放目录

```bash
cd AstrBot/data/plugins
git clone https://github.com/czacdat/astrbot_plugin_group_dm_guide.git
```

⚠️ 手动放进去后**必须重启 AstrBot**（它只在启动时扫描插件目录）。

### 依赖

```
AstrBot >= 4.24.0
平台：aiocqhttp（QQ / NapCat / Lagrange 等）
```

本插件依赖 `bot.send_private_msg` 发送私聊，**仅 aiocqhttp 平台完整可用**；
其它平台会退化为通用接口，群临时会话不可用。

## 配置

面板 → 插件管理 → 群聊高频提问引导 → 配置

| 配置 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `enable` | bool | `True` | 总开关 |
| `redirect_reply` | bool | `True` | **开**：把回答私聊发给提问者；**关**：改为在群里发引导语 |
| `enable_temp_session` | bool | `True` | 好友私聊失败时，尝试群临时会话 |
| `min_redirect_chars` | int | `10` | 回答短于这个字数就不转私聊（0 = 不限制） |
| `intercept_on_fail` | bool | `True` | 私聊发不出去时也拦截群回复 |
| `blocked_notice` | text | `（这些私聊说吧，群里不方便）` | 拦截时群里说的话，留空则不出声 |
| `group_notice` | text | `（这里不方便说，私聊发你了）` | 转私聊成功后群里留的话 |
| `fallback_notice` | text | `（加我好友才能私聊哦）` | 关闭拦截时退回群里的提示 |
| `trigger_count` | int | `3` | 触发次数 |
| `window_minutes` | int | `10` | 统计时间窗（分钟） |
| `cooldown_minutes` | int | `30` | 触发后多久内继续走私聊（0 = 不冷却） |
| `block_during_cooldown` | bool | `True` | 仅 `redirect_reply=false` 时生效：冷却期完全不响应 |
| `message` | text | `……你问题有点多啦，群里说不太方便，加我好友私聊说吧。` | 引导语（仅 `redirect_reply=false` 时用）；支持 `{count}` `{minutes}` |
| `exempt_admins` | bool | `True` | 管理员不受限制 |
| `whitelist` | list | `[]` | 用户白名单（QQ 号） |
| `group_whitelist` | list | `[]` | 生效的群号，留空 = 所有群 |
| `dry_run` | bool | `False` | 只记日志，不发私聊、不拦截 |

### 推荐配置

**刚上线先观察**：

```
dry_run = 开
```

跑一两天，日志里会写 `[群聊引导] 触发: group=... user=... 第3次 dry_run=True`，
看看它抓的人是不是你想引导的，再关掉 `dry_run` 正式启用。

**正式用**：

```
redirect_reply    = 开
min_redirect_chars = 10
intercept_on_fail = 开
exempt_admins     = 开
```

## 工作原理

插件挂两个钩子：

```
on_llm_request    —— 统计「群 + 人」在时间窗内的触发次数，超阈值就打标记
on_llm_response   —— 带标记的那一轮，把回答私聊发出去，然后清空群回复
```

只统计**机器人真要回话**的时机，普通群聊不计数。

### 送达逻辑

按顺序尝试，任一成功即止：

```
① bot.send_private_msg(user_id, group_id)   群临时会话（非好友可用）
② bot.send_private_msg(user_id)             好友私聊
③ context.send_message(umo, chain)          其它平台兜底
↓↓ 全部失败
④ intercept_on_fail = 开 → 拦掉群回复，只留 blocked_notice
   intercept_on_fail = 关 → 退回群里正常回答
```

> 为什么优先用 `bot` 直连而不是 `context.send_message`？
> 因为 `context.send_message()` 的返回值只代表「找到了对应平台」，
> 不代表真的发出去。bot 直连失败会抛异常，判断才可靠。

## 已知限制

| 限制 | 说明 |
|---|---|
| **群临时会话有时效** | 只能在临时会话有效期内发送。过期后 QQ 返回 `retcode=100, err=发送失败，请先添加对方为好友`。所以非好友是「半闭环」：他发得出消息，机器人不一定回得了 |
| 非好友收不到回答 | 如果私聊和临时会话都失败，且 `intercept_on_fail=开`，那这个人在群里就收不到回答（只有提示）。这是刻意的设计，把引导压力给到对方 |
| 只在 aiocqhttp 生效 | 其它平台没有 `send_private_msg`，会退化为通用接口 |
| 统计的是「触发次数」 | 插件不区分「恶意刷屏」和「认真连问三个问题」，所以建议先 `dry_run` 观察 |

## 常见问题

**Q：为什么它没反应？**
A：检查三点——① 消息真的叫醒机器人了吗（@ 或唤醒词）；② 这个人在 `admins_id` 里吗（管理员默认豁免）；③ 是不是在 `group_whitelist` 之外。

**Q：为什么私聊发不出去？**
A：对方不是好友，且群临时会话过期或被 QQ 限制。日志里会有 `请先添加对方为好友`。

**Q：为什么回答没有转私聊？**
A：可能被 `min_redirect_chars` 门槛拦下了（回答太短），或者这一轮是工具调用轮（会被跳过，只转最终回答）。

**Q：能只对某些人生效吗？**
A：用 `group_whitelist` 限定群，用 `whitelist` 排除个别人。

## 开发

### 运行测试

测试用 `unittest`，不需要 AstrBot 环境（内部会 stub 掉 `astrbot` 模块）：

```bash
python tests/test_plugin.py
```

覆盖：阈值计数、转私聊、临时会话兜底、字数门槛、失败拦截、工具轮跳过、管理员/白名单豁免、dry_run。

### 项目结构

```
astrbot_plugin_group_dm_guide/
├── main.py              插件主逻辑
├── _conf_schema.json    配置项定义（面板自动生成表单）
├── metadata.yaml        插件元信息
├── README.md
├── CHANGELOG.md
├── LICENSE
└── tests/
    └── test_plugin.py   离线单元测试
```

### 涉及到的 AstrBot 接口

```
@filter.on_llm_request(priority=5)     每轮 LLM 请求前
@filter.on_llm_response(priority=-100) 每轮 LLM 响应后（工具调用轮不触发）
event.set_extra / get_extra            在同一个事件里传状态
event.should_call_llm(True)            禁止默认 LLM 链路（参数名有误导）
event.stop_event()                     终止事件传播
response.completion_text = ""          清空后即可阻止群回复
bot.send_private_msg(...)              OneBot v11 发私聊（带 group_id = 临时会话）
```

## 关于作者

**本插件由 AI 编写。**

代码、测试、文档均由 AI 助手（DeepSeek Harness 驱动的编码代理）根据需求生成，
人类负责提需求、验收和发布。经过 4 轮迭代（v1.0.0 → v1.3.1）和线上实测修正。

使用中发现问题欢迎提 Issue，但请理解维护者不是传统意义上的"作者"。

## License

[MIT](LICENSE)
