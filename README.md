# astrbot_plugin_wuwa_echo

鸣潮（Wuthering Waves）声骸评分 AstrBot 插件。**纯关键词触发，不走 LLM 推理**，端到端 2-4 秒返回。

## 用法

| 你说 | bot 反应 | 耗时 |
|---|---|---|
| *(发声骸图)* + "千咲评分" | 直接评分 | 2-4s |
| *(发整套图)* + "维妈整套打分" | 整套评分（昵称自动解析） | 2-4s |
| 先发图 → 再 @ bot 说"评分千咲" | 用刚才的图评分 | 2-4s |
| @ bot "评分今汐"（没图） → 后续发图 | 自动续上评分 | 2-4s |
| *(只发图，无任何关键词)* | **完全静默**，后台缓存 | 0s |
| @ bot "你好" | 走 AstrBot 自带 LLM 闲聊 | — |

## 触发条件

群里 @ bot（私聊无需）+ 消息满足 **意图关键词 + 角色名 + 图片** 三件套即触发。

**意图关键词**（任一命中即可）：

> 评分、打分、评级、几分、多少分、鉴定、词条、品质、怎么样、好不好、强不强、echo、Echo、声骸、声骇、声海

整套模式补充关键词：整套、一套、全部、5件、总评、全套

**角色名**：50 个规范角色名 + 500+ 个玩家昵称 / 拼音缩写 / 英文名都能识别。

**图片**：当前消息附带，或者最近 5 分钟内同会话发过的图（自动缓存）。

三件套缺一个，bot 会反问；缺多个，反问一次记下所有给定的，等用户补齐自动续上。

## 工作原理

```
用户消息 → @filter.event_message_type 监听器 → 关键词扫描 → OCR → 评分 → 回复
            ↑
       不经过 LLM agent runner,不经过 function calling
```

省掉了：
- LLM 决定"该不该调工具"的 1-2 秒
- agent loop 收尾的 5-13 秒
- 自然语言推理误判（如把"千咲"OCR 成"千联"再去查别名）

代价：
- 不会自然语言推理（用户必须命中关键词）
- 多模态闲聊（"这是哪个角色"）这种推理类问题本插件不接，由 AstrBot 默认 LLM 路径处理

## 评分算法

双指标制，满分 50：

- **词条种类分（0-25）**：5 条副词条权重和 / 满潜力（5×3=15），归一化到 25
- **词条数值分（0-25）**：每条副词条 `(当前 / 满级) × 权重` 加权平均，归一化到 25

权重等级：

| 等级 | 含义 |
|---|---|
| 3 | 核心（暴击 / 暴击伤害 / 角色专属伤害加成） |
| 2 | 重要（攻击% / 共鸣效率 / 主技能伤害加成） |
| 1 | 次要（固定攻击 / 副技能伤害加成） |
| 0 | 无用 |

评级阈值（可在 `weights.json` 改）：

| 评级 | 阈值 |
|---|---|
| ACE | ≥ 45 |
| SSS | ≥ 35 |
| SS | ≥ 25 |
| S | ≥ 18 |
| A | ≥ 10 |
| N | < 10 |

## 数据来源

- **角色权重**：[anyul.cn](https://www.anyul.cn) 50 个角色的副词条权重（v0.2.0 一次性爬取）
- **角色别名**：[XutheringWavesUID](https://github.com/loping151/XutheringWavesUID) 的 `char_alias.json`（500+ 条玩家昵称）
- **图片 OCR**：通过 `vision_provider_id` 指定的多模态 provider 完成；默认走 AstrBot 当前 provider 兜底
  - **推荐配置**：SiliconFlow + `Qwen/Qwen3-VL-8B-Instruct`（2-4 秒识图，免费额度足够）

## OCR 容错

| 场景 | 处理 |
|---|---|
| OCR 把"攻击 150"（COST 4 第二个主词条）误识为副词条 | 数值上限兜底，超过副词条最大值 1.05 倍直接丢弃 |
| 图片模糊，副词条少于 5 条 | 报错"识别到 X 条副词条,不足 5 条,请发送清晰截图" |
| COST 3/4 没识别出主词条 | 报错"图片可能模糊" |
| 角色名识别不到 | 反问"请告诉我角色名" |

## 安装

### 方式 1：AstrBot WebUI 直装

```
https://github.com/yunyancuo/astrbot_plugin_wuwa_echo
```

国内服务器拉不下来时换 GitHub 代理：

```
https://gh-proxy.com/https://github.com/yunyancuo/astrbot_plugin_wuwa_echo
```

### 方式 2：服务器手动 clone

```bash
docker exec astrbot bash -c "cd /AstrBot/data/plugins && git clone https://gh-proxy.com/https://github.com/yunyancuo/astrbot_plugin_wuwa_echo.git"
docker restart astrbot
```

## 配置

WebUI → 插件管理 → astrbot_plugin_wuwa_echo → 配置：

| 字段 | 说明 | 推荐值 |
|---|---|---|
| `default_character` | 未匹配角色时的兜底模板 | `generic_crit` |
| `vision_provider_id` | 视觉 provider 的 AstrBot ID | `siliconflow/Qwen/Qwen3-VL-8B-Instruct` |
| `show_substat_breakdown` | 是否在回复里展示副词条明细 | `true` |
| `vision_prompt_lang` | OCR 提示语语言 | `zh` |

`vision_provider_id` 必须填一个**多模态**模型的 ID（能看图）。否则 OCR 会被发到文本模型导致 400 错。

## 自定义

### 加角色权重

`data/weights.json` 的 `characters` 段：

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

`data/aliases.json`：

```json
"今汐": ["今汐", "jx", "xx", "汐汐", "龙女", "你给的新昵称"]
```

### 改完热重载

```bash
docker exec astrbot bash -c "cd /AstrBot/data/plugins/astrbot_plugin_wuwa_echo && git pull"
docker restart astrbot
```

或 WebUI 里点"重载插件"。

## 目录结构

```
astrbot_plugin_wuwa_echo/
├── main.py                # 监听器入口
├── metadata.yaml
├── _conf_schema.json
├── requirements.txt
├── README.md
├── core/
│   ├── models.py          # pydantic 数据模型
│   ├── ocr.py             # vision provider 调用 + OCR 兜底校验
│   ├── scorer.py          # 评分核心
│   └── resolver.py        # 角色名 / 别名 / 模糊匹配
└── data/
    ├── weights.json       # 角色权重 + 副词条上限 + 评级阈值
    └── aliases.json       # 角色 → 别名列表
```

## 已知限制

- 多模态模型识别精度依赖你选的 vision_provider；建议至少 7B 起步
- 主词条不参与评分，只识别（声骸主词条固定，决定品质的是副词条）
- 套装合鸣效果不参与评分
- 只识别 +25 满级声骸的 5 条副词条；< 5 条会报错（设计如此，免得分数不准）

## 版本历史

- **v0.4.0** — 全砍 LLM，纯监听器实现，端到端 2-4 秒
- v0.3.x — 基于 `@filter.llm_tool` 的 LLM agent 路径（用户感知 5s，但 tail 5-15s）
- v0.2.0 — 50 角色权重 + 500+ 昵称
- v0.1.0 — 初版，4 个通用模板
