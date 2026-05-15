# astrbot_plugin_wuwa_echo

> 鸣潮（Wuthering Waves）声骸评分 AstrBot 插件 · **纯监听器 + 多模态 OCR · 端到端 2–4 秒**

[![version](https://img.shields.io/badge/version-0.4.3-blue)](https://github.com/yunyancuo/astrbot_plugin_wuwa_echo/releases) [![AstrBot](https://img.shields.io/badge/AstrBot-v4.x-green)](https://docs.astrbot.app)

发图 + 说"千咲评分"，3 秒拿到 SSS 评级与逐条词条分析。不走 LLM 推理，不依赖 function calling，关键词命中即触发。

---

## 用法一览

| 你说的 | bot 回的 | 耗时 |
|---|---|---|
| *（发图）* + `千咲评分` | 评分结果 | **2–4s** |
| *（发整套图）* + `维妈整套打分` | 整套评分（昵称自动解析） | **2–4s** |
| 先发图 → 再 `@bot 评分千咲` | 用刚才的图评分 | **2–4s** |
| `@bot 评分今汐`（没图） → 后续单独发图 | 自动续上评分 | **2–4s** |
| *（只发图，无关键词）* | **完全静默**，后台缓存 5 分钟 | 0s |
| `@bot 你好` | 走 AstrBot 自带 LLM 聊天 | — |

---

## 触发条件

群聊需要 @ bot；私聊直接说即可。命中下面三件套触发评分：

| 要素 | 命中规则 |
|---|---|
| **意图关键词** | 文本里出现任一：`评分` `打分` `评级` `几分` `多少分` `鉴定` `词条` `品质` `怎么样` `好不好` `强不强` `echo` `声骸` `声骇` `声海` |
| **整套模式**（可选） | 额外命中：`整套` `一套` `全部` `5件` `总评` `全套` → 走整套评分 |
| **角色名** | 50 个规范名 + 500+ 玩家昵称 / 拼音缩写 / 英文名都能识别 |
| **图片** | 当前消息附带，或同会话 5 分钟内缓存过的图 |

三件套缺一项 bot 会反问；缺多项一次反问，等用户**补齐自动续办**——补充的消息无需再 @ bot。

---

## 工作原理

```text
用户消息 ─▶ @filter.event_message_type 监听器 ─▶ 关键词扫描 ─▶ 视觉 OCR ─▶ 评分 ─▶ 回复
                       │
                       └─ stop_event + _has_send_oper + call_llm
                          ↑ 阻止 AstrBot 的 LLM agent 并发跑出"幽灵 tail"
```

**为什么不走 LLM tool？**

| 路径 | 用户感知 | "幽灵 tail" | 自然语言 |
|---|---|---|---|
| `@filter.llm_tool`（v0.3.x） | 4.8s | 5–18s | ✅ |
| 当前监听器路径（v0.4.x） | **2–4s** | **~0s** | ❌（只识别关键词） |

LLM tool 让 DeepSeek 决定该不该调评分工具，工具完后还得让 LLM 决定"要不要继续"——加起来一来一回 6–20 秒。砍掉这层，速度直接砍半。代价是用户必须命中关键词（实测日常聊天足够，没人会用花式句法问"这件声骸价值几何"）。

---

## 评分算法

双指标制，满分 50：

- **词条种类分（0–25）**：5 条副词条权重之和 / 满潜力 5×3 = 15，归一化到 25
- **词条数值分（0–25）**：每条副词条 `(当前 / 满级) × 权重` 加权平均，归一化到 25

权重等级语义（在 `data/weights.json` 配置）：

| 等级 | 含义 |
|---|---|
| **3** | 核心（暴击 / 暴击伤害 / 角色专属伤害加成） |
| **2** | 重要（攻击% / 共鸣效率 / 主技能伤害加成） |
| **1** | 次要（固定攻击 / 副技能伤害加成） |
| **0** | 无用 |

评级阈值：

| 评级 | 分数 |
|---|---|
| ACE | ≥ 45 |
| SSS | ≥ 35 |
| SS  | ≥ 25 |
| S   | ≥ 18 |
| A   | ≥ 10 |
| N   | <  10 |

---

## OCR 容错

| 场景 | 处理 |
|---|---|
| 把 COST 4 的固定主词条 `攻击 150` 误识为副词条 | 数值上限兜底，超过副词条上限 1.05 倍直接丢弃 |
| 图片模糊，副词条少于 5 条 | 报错 *"识别到 X 条副词条,不足 5 条,请发清晰截图"* |
| COST 3/4 没识别出主词条 | 报错 *"图片可能模糊"* |
| 角色名识别不到 | 反问 *"请告诉我角色名"*（不静默卡死） |

---

## 数据来源

| 数据 | 来源 | 用途 |
|---|---|---|
| 50 角色副词条权重 | [anyul.cn](https://www.anyul.cn) | 算分核心 |
| 500+ 玩家昵称 / 拼音 | [XutheringWavesUID](https://github.com/loping151/XutheringWavesUID) `char_alias.json` | 模糊匹配 |
| 图片 OCR | 你 AstrBot 配置的视觉 provider | 副词条识别 |

**推荐视觉 provider**：

| Provider | 模型 | 速度 | 价格 |
|---|---|---|---|
| **SiliconFlow** ⭐ | `Qwen/Qwen3-VL-8B-Instruct` | 1.5–3s | 免费额度送 ¥14 |
| 阿里云百炼 | `qwen-vl-plus` | 2–5s | ~¥0.003/张 |
| 智谱 | `glm-4.6v-flash` | 2–8s | 免费 |

---

## 安装

### 方式 A · WebUI 直装（推荐）

AstrBot WebUI → 插件管理 → 安装插件 → 粘贴 URL：

```text
https://github.com/yunyancuo/astrbot_plugin_wuwa_echo
```

> 国内服务器若拉不动 GitHub，换 [gh-proxy](https://gh-proxy.com/) 反代：
> `https://gh-proxy.com/https://github.com/yunyancuo/astrbot_plugin_wuwa_echo`

### 方式 B · 服务器手动 clone

```bash
docker exec astrbot bash -c "cd /AstrBot/data/plugins && \
  git clone https://gh-proxy.com/https://github.com/yunyancuo/astrbot_plugin_wuwa_echo.git"
docker restart astrbot
```

---

## 配置

WebUI → 插件管理 → `astrbot_plugin_wuwa_echo` → 配置：

| 字段 | 说明 | 推荐值 |
|---|---|---|
| `default_character` | 未匹配角色时的兜底模板 | `generic_crit` |
| **`vision_provider_id`** | 视觉 provider 的 AstrBot ID | `siliconflow/Qwen/Qwen3-VL-8B-Instruct` |
| `show_substat_breakdown` | 是否展示副词条明细 | `true` |
| `vision_prompt_lang` | OCR 提示语言（zh/en） | `zh` |

> ⚠️ `vision_provider_id` **必须**指向能看图的多模态模型。指向 DeepSeek 这种文本模型会 400 报错。
>
> 查 provider 真实 ID：
> ```bash
> docker exec astrbot python3 -c "import json;d=json.load(open('/AstrBot/data/cmd_config.json',encoding='utf-8-sig'));[print(p.get('id')) for p in d.get('provider',[])]"
> ```

---

## 自定义

### 加角色权重

编辑 `data/weights.json` 的 `characters` 段：

```json
"我家角色": {
  "_desc": "重击型雷主C",
  "暴击": 3, "暴击伤害": 3,
  "重击伤害加成": 3,
  "攻击%": 2, "攻击": 1,
  "共鸣技能伤害加成": 2,
  "共鸣解放伤害加成": 1,
  "共鸣效率": 0, "普攻伤害加成": 0,
  "生命%": 0, "防御%": 0, "生命": 0, "防御": 0
}
```

### 加昵称

编辑 `data/aliases.json`：

```json
"今汐": ["今汐", "jx", "xx", "汐汐", "龙女", "你起的新昵称"]
```

### 改完热重载

```bash
docker exec astrbot bash -c "cd /AstrBot/data/plugins/astrbot_plugin_wuwa_echo && \
  git pull https://gh-proxy.com/https://github.com/yunyancuo/astrbot_plugin_wuwa_echo.git main"
docker restart astrbot
```

或 WebUI 点「重载插件」。

---

## 目录结构

```text
astrbot_plugin_wuwa_echo/
├── main.py                # 监听器入口 + LLM 阻断逻辑
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
├── README.md
├── core/
│   ├── models.py          # pydantic 数据模型
│   ├── ocr.py             # vision provider 调用 + 副词条校验
│   ├── scorer.py          # 评分核心
│   └── resolver.py        # 角色名 / 别名 / 模糊匹配
└── data/
    ├── weights.json       # 角色权重 + 副词条上限 + 评级阈值
    └── aliases.json       # 角色 → 别名列表
```

---

## 已知限制

- 多模态识别精度看你选的 vision provider；建议 **7B 起步**
- 主词条不参与评分（声骸主词条相对固定，真正决定品质的是副词条）
- 套装合鸣效果不参与评分
- 仅支持 **+25 满级**声骸（5 条副词条），未满级会触发"模糊图"报错

---

## 版本历史

| 版本 | 关键变化 |
|---|---|
| **v0.4.3** ⭐ | 设置 `_has_send_oper` + `call_llm` 标志彻底阻断 LLM 阶段，幽灵 tail 清零 |
| v0.4.2 | stop_event 后 `asyncio.sleep(0)` 让出 tick |
| v0.4.1 | stop_event 提前到 OCR 之前 |
| v0.4.0 | 全砍 LLM tool，纯监听器架构 |
| v0.3.x | `@filter.llm_tool` 路径（用户感知 5s，tail 5–18s） |
| v0.2.0 | 50 角色权重 + 500+ 昵称 |
| v0.1.0 | 初版，4 个通用模板 |

---

## 致谢

- 角色权重 · [anyul.cn](https://www.anyul.cn)
- 角色昵称 · [loping151/XutheringWavesUID](https://github.com/loping151/XutheringWavesUID)
- AstrBot 框架 · [AstrBotDevs/AstrBot](https://github.com/AstrBotDevs/AstrBot)

## License

MIT
