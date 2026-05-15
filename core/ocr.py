from __future__ import annotations

import json
import re
from typing import List, Optional

from .models import Echo, EchoSet, SubStat


SINGLE_ECHO_PROMPT_ZH = """你是鸣潮(Wuthering Waves)声骸识别助手。请仔细识别图片中的声骸信息,严格按 JSON 输出,不要任何额外文字。

需要识别的字段:
- cost: 整数,COST 值(1/3/4)
- set_name: 字符串,合鸣效果套装名(如"凝夜白霜""熔山裂谷")。识别不到填 null
- main_stat: 字符串,主词条名(如"暴击""攻击%""属性伤害加成")。COST 1 通常没有主词条,填 null
- main_stat_value: 数字,主词条数值。百分比类型只填数字部分,如 22.0 表示 22%
- level: 整数,强化等级(0-25)
- sub_stats: 数组,每项 {"name": "副词条名", "value": 数字}

副词条名称必须用下列**标准名**之一:
暴击, 暴击伤害, 攻击%, 生命%, 防御%, 共鸣效率, 普攻伤害加成, 重击伤害加成, 共鸣技能伤害加成, 共鸣解放伤害加成, 攻击, 生命, 防御

(注意:"攻击%"指百分比加成,"攻击"指固定数值;"生命""防御"同理。)

仅输出形如下面的 JSON,不要 markdown 代码块:
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


def parse_single(raw_text: str) -> Echo:
    data = json.loads(_extract_json(raw_text))
    subs = [SubStat(name=s["name"], value=float(s["value"]))
            for s in data.get("sub_stats", []) if s.get("name")]
    return Echo(
        cost=int(data.get("cost", 4)),
        set_name=data.get("set_name"),
        main_stat=data.get("main_stat"),
        main_stat_value=data.get("main_stat_value"),
        sub_stats=subs,
        level=int(data.get("level", 0)),
    )


def parse_set(raw_text: str) -> EchoSet:
    data = json.loads(_extract_json(raw_text))
    echoes_raw = data.get("echoes", [])
    if not isinstance(echoes_raw, list):
        raise ValueError("echoes 字段必须是数组")

    echoes: List[Echo] = []
    for item in echoes_raw:
        subs = [SubStat(name=s["name"], value=float(s["value"]))
                for s in item.get("sub_stats", []) if s.get("name")]
        echoes.append(Echo(
            cost=int(item.get("cost", 4)),
            set_name=item.get("set_name"),
            main_stat=item.get("main_stat"),
            main_stat_value=item.get("main_stat_value"),
            sub_stats=subs,
            level=int(item.get("level", 0)),
        ))
    return EchoSet(echoes=echoes)


async def vision_recognize(provider, image_url: str, prompt: str) -> str:
    """调用 AstrBot LLM provider 的多模态接口识别图片。"""
    response = await provider.text_chat(
        prompt=prompt,
        image_urls=[image_url],
    )
    return getattr(response, "completion_text", "") or str(response)
