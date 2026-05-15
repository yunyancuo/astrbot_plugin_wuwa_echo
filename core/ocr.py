from __future__ import annotations

import json
import re
from typing import List

from .models import Echo, EchoSet, SubStat


SINGLE_ECHO_PROMPT_ZH = """你是鸣潮(Wuthering Waves)声骸识别助手。仔细识别图片中的声骸信息,严格按 JSON 输出,不要任何额外文字。

# 主词条 vs 副词条的区分(极其重要!)

游戏 UI 里:
- **主词条**: 在卡片上方,**不带 + 前缀**的大字条目。
  - COST 4: **有 2 个主词条** = 1 个可变主词条(从 暴击/暴击伤害/属性伤害加成/治疗效果加成 选) + 1 个固定的"攻击 150"。
  - COST 3: **1 个主词条**(从 攻击%/生命%/防御%/共鸣效率/属性伤害加成 选)。
  - COST 1: **1 个固定主词条** = 攻击/生命/防御 之一(固定数值,无加成)。
- **副词条**: 卡片下方,**每条前面带 + 号**的小字条目,共 1-5 条。

**只把带 + 前缀的条目放进 sub_stats!** 不带 + 的全是主词条,不要写进 sub_stats。
最常见的错误就是把 COST 4 的固定"攻击 150"主词条当成副词条 —— 千万别犯。

# 百分比 vs 固定值

游戏 UI 中,值后带 % 的代表百分比加成。副词条名要用对应的百分比版:
- 显示"攻击 9.4%"  →  副词条名 = "攻击%",值 = 9.4
- 显示"攻击 30"    →  副词条名 = "攻击",   值 = 30
- 显示"生命 10.9%" →  副词条名 = "生命%",值 = 10.9
- 显示"防御 11.6%" →  副词条名 = "防御%",值 = 11.6

# 副词条数值上限参考(超出说明你认错了主副)

暴击 ≤ 10.5  |  暴击伤害 ≤ 21  |  攻击% ≤ 11.6  |  生命% ≤ 11.6  |  防御% ≤ 14.7
共鸣效率 ≤ 11.6  |  各类伤害加成 ≤ 11.6
攻击 ≤ 60  |  生命 ≤ 580  |  防御 ≤ 70

# 输出字段

- cost: 整数,COST 值(1/3/4)
- set_name: 字符串,合鸣效果套装名(如"凝夜白霜""熔山裂谷"). 识别不到填 null
- main_stat: 字符串,**可变那个主词条**的名字(COST 4 时不是固定的"攻击 150")
- main_stat_value: 数字,主词条数值
- level: 整数,强化等级(0-25,通常 +25)
- sub_stats: 数组(1-5 项),每项 {"name": "副词条标准名", "value": 数字}

副词条**标准名**只能从下面这 13 个里选:
暴击, 暴击伤害, 攻击%, 生命%, 防御%, 共鸣效率, 普攻伤害加成, 重击伤害加成, 共鸣技能伤害加成, 共鸣解放伤害加成, 攻击, 生命, 防御

仅输出 JSON,不要 markdown 代码块,不要任何解释:
{"cost": 4, "set_name": "凝夜白霜", "main_stat": "暴击伤害", "main_stat_value": 44.0, "level": 25, "sub_stats": [{"name": "暴击", "value": 9.3}, {"name": "攻击%", "value": 10.5}]}
"""

SET_ECHO_PROMPT_ZH = """你是鸣潮(Wuthering Waves)声骸识别助手。图片包含一套(最多 5 件)声骸,请逐个识别,严格按 JSON 输出,不要任何额外文字。

输出格式:
{"echoes": [
  {"cost": 4, "set_name": "...", "main_stat": "...", "main_stat_value": 0.0, "level": 25, "sub_stats": [{"name": "...", "value": 0.0}, ...]},
  ...
]}

副词条名称必须用下列标准名之一:
暴击, 暴击伤害, 攻击%, 生命%, 防御%, 共鸣效率, 普攻伤害加成, 重击伤害加成, 共鸣技能伤害加成, 共鸣解放伤害加成, 攻击, 生命, 防御

百分比类型只填数字部分(如 9.3 表示 9.3%)。识别不到的字段填 null。
"""


# 副词条数值上限(超过这个值说明 OCR 误把主词条当副词条了, 应丢弃)
SUBSTAT_MAX_VALUE = {
    "暴击": 10.5, "暴击伤害": 21.0,
    "攻击%": 11.6, "生命%": 11.6, "防御%": 14.7,
    "共鸣效率": 11.6,
    "普攻伤害加成": 11.6, "重击伤害加成": 11.6,
    "共鸣技能伤害加成": 11.6, "共鸣解放伤害加成": 11.6,
    "攻击": 60.0, "生命": 580.0, "防御": 70.0,
}


def _extract_json(text: str) -> str:
    """从 LLM 输出中提取第一个 JSON 对象。"""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        raise ValueError(f"LLM 输出中找不到 JSON: {text[:200]}")
    return match.group(0)


def _sanitize_substats(raw_subs) -> List[SubStat]:
    """从 OCR 原始输出里筛出合法的副词条。

    丢弃规则:
      - 名字不在 13 个标准名清单里
      - 数值超过该名义的副词条上限(常见误识:COST 4 固定的"攻击 150"被当成副词条)
    """
    if not isinstance(raw_subs, list):
        return []
    out: List[SubStat] = []
    for s in raw_subs:
        if not isinstance(s, dict):
            continue
        name = s.get("name")
        if not name or name not in SUBSTAT_MAX_VALUE:
            continue
        try:
            value = float(s.get("value", 0))
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        if value > SUBSTAT_MAX_VALUE[name] * 1.05:
            # 留 5% 容差防止浮点边缘,超过就丢
            continue
        out.append(SubStat(name=name, value=value))
    return out


def _validate_echo(echo: Echo, *, idx: int = -1) -> None:
    """校验声骸完整性。+25 声骸应有 1 个可变主词条 + 5 个副词条。
    不达标抛 ValueError(可能是图片模糊或截图不完整)。
    """
    label = f"第 {idx + 1} 件" if idx >= 0 else ""
    if len(echo.sub_stats) < 5:
        raise ValueError(
            f"{label}识别到 {len(echo.sub_stats)} 条副词条,不足 5 条。"
            f"请发送清晰的完整声骸截图(+25 声骸应有 5 条副词条)。"
        )
    if echo.cost in (3, 4) and not echo.main_stat:
        raise ValueError(
            f"{label}没识别出 COST {echo.cost} 声骸的主词条。"
            f"图片可能模糊,请发送清晰的截图。"
        )


def parse_single(raw_text: str) -> Echo:
    data = json.loads(_extract_json(raw_text))
    subs = _sanitize_substats(data.get("sub_stats", []))
    echo = Echo(
        cost=int(data.get("cost", 4)),
        set_name=data.get("set_name"),
        main_stat=data.get("main_stat"),
        main_stat_value=data.get("main_stat_value"),
        sub_stats=subs,
        level=int(data.get("level", 0)),
    )
    _validate_echo(echo)
    return echo


def parse_set(raw_text: str) -> EchoSet:
    data = json.loads(_extract_json(raw_text))
    echoes_raw = data.get("echoes", [])
    if not isinstance(echoes_raw, list):
        raise ValueError("echoes 字段必须是数组")

    echoes: List[Echo] = []
    for i, item in enumerate(echoes_raw):
        subs = _sanitize_substats(item.get("sub_stats", []))
        echo = Echo(
            cost=int(item.get("cost", 4)),
            set_name=item.get("set_name"),
            main_stat=item.get("main_stat"),
            main_stat_value=item.get("main_stat_value"),
            sub_stats=subs,
            level=int(item.get("level", 0)),
        )
        _validate_echo(echo, idx=i)
        echoes.append(echo)
    return EchoSet(echoes=echoes)


async def vision_recognize(provider, image_url: str, prompt: str) -> str:
    """调用 AstrBot LLM provider 的多模态接口识别图片。"""
    response = await provider.text_chat(
        prompt=prompt,
        image_urls=[image_url],
    )
    return getattr(response, "completion_text", "") or str(response)
