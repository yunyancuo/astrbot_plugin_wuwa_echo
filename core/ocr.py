from __future__ import annotations

import base64
import json
import re
from typing import List

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


# 副词条标准名清单(按长度倒序,先匹配长名避免 "攻击%" 被 "攻击" 抢走)
SUBSTAT_NAMES = sorted([
    "暴击", "暴击伤害",
    "攻击%", "攻击",
    "生命%", "生命",
    "防御%", "防御",
    "共鸣效率",
    "普攻伤害加成", "重击伤害加成",
    "共鸣技能伤害加成", "共鸣解放伤害加成",
], key=len, reverse=True)

# COST 与 levels 文本中可能出现的形式
_COST_PATTERN = re.compile(r"COST\s*[:：]?\s*(\d)|(?:^|\s)([134])\s*费", re.IGNORECASE)
_LEVEL_PATTERN = re.compile(r"\+(\d{1,2})|Lv\.?\s*(\d{1,2})", re.IGNORECASE)


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
    """通过 AstrBot 的多模态 LLM provider 识别图片。"""
    response = await provider.text_chat(
        prompt=prompt,
        image_urls=[image_url],
    )
    return getattr(response, "completion_text", "") or str(response)


# ===================== GLM-OCR 专用通道 =====================

ZHIPU_OCR_ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/layout_parsing"


async def _image_to_data_uri(image_url: str) -> str:
    """把任意来源的图片(http URL / file:// / 本地路径)转成 data URI。"""
    import httpx
    if image_url.startswith("file://"):
        path = image_url[7:]
        with open(path, "rb") as f:
            raw = f.read()
    elif image_url.startswith(("/", ".")) or (len(image_url) > 2 and image_url[1] == ":"):
        # /abs/path, ./rel/path, C:\\... 一律按本地文件读
        with open(image_url, "rb") as f:
            raw = f.read()
    else:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            r = await client.get(image_url)
            r.raise_for_status()
            raw = r.content
    # 检测 mime
    if raw[:8].startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
    elif raw[:6] in (b"GIF87a", b"GIF89a"):
        mime = "image/gif"
    elif raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        mime = "image/webp"
    else:
        mime = "image/jpeg"
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _extract_text_from_glm_response(result: dict) -> str:
    """智谱 GLM-OCR 响应字段名未公开,尝试多种常见路径取出 markdown/text。"""
    candidates: list[tuple[list[str], object]] = []

    def collect(node, path):
        if isinstance(node, dict):
            for k, v in node.items():
                collect(v, path + [k])
        elif isinstance(node, list):
            for i, v in enumerate(node):
                collect(v, path + [str(i)])
        else:
            candidates.append((path, node))

    collect(result, [])

    # 优先级:含 markdown > text > content,且值是非空字符串
    def score(path, val):
        if not isinstance(val, str) or not val.strip():
            return -1
        s = 0
        path_str = ".".join(path).lower()
        if "markdown" in path_str:
            s += 100
        if "text" in path_str:
            s += 50
        if "content" in path_str:
            s += 30
        s += len(val) // 100  # 越长(信息越多)越优先
        return s

    scored = [(score(p, v), p, v) for p, v in candidates]
    scored = [t for t in scored if t[0] > 0]
    if not scored:
        # 兜底:把整个响应 dump 出来让 regex 自己找
        return json.dumps(result, ensure_ascii=False)
    scored.sort(key=lambda t: -t[0])
    return scored[0][2]  # type: ignore


def _parse_echo_from_text(text: str) -> Echo:
    """从 OCR 出的纯文本/markdown 解析出 Echo 对象。

    策略:
      - 副词条名命中后,向后 0-40 字符内找最近的数字
      - cost / level 用正则匹配
      - 同名副词条只取第一次出现的
    """
    sub_stats: list[SubStat] = []
    seen_names: set[str] = set()

    for name in SUBSTAT_NAMES:
        pos = 0
        while True:
            idx = text.find(name, pos)
            if idx < 0:
                break
            if name in seen_names:
                break
            # 在 name 后面 0-40 字符内找数字
            tail = text[idx + len(name): idx + len(name) + 40]
            m = re.search(r"([0-9]+(?:\.[0-9]+)?)(\s*%)?", tail)
            if m:
                try:
                    value = float(m.group(1))
                    sub_stats.append(SubStat(name=name, value=value))
                    seen_names.add(name)
                except ValueError:
                    pass
                break
            pos = idx + len(name)

    # cost
    cost = 4
    m = _COST_PATTERN.search(text)
    if m:
        cost_str = m.group(1) or m.group(2)
        try:
            cost = int(cost_str)
        except (ValueError, TypeError):
            pass

    # level
    level = 0
    m = _LEVEL_PATTERN.search(text)
    if m:
        try:
            level = int(m.group(1) or m.group(2) or 0)
        except (ValueError, TypeError):
            pass

    return Echo(
        cost=cost,
        set_name=None,
        main_stat=None,
        main_stat_value=None,
        level=level,
        sub_stats=sub_stats,
    )


async def glm_ocr_recognize_single(api_key: str, image_url: str) -> Echo:
    """调智谱 GLM-OCR 识别单件声骸,返回 Echo 对象。

    比走 vision_provider 的多模态接口快很多(实测 1.5-3s vs 30s+)。
    """
    import httpx
    data_uri = await _image_to_data_uri(image_url)
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            ZHIPU_OCR_ENDPOINT,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"model": "glm-ocr", "file": data_uri},
        )
        r.raise_for_status()
        result = r.json()

    text = _extract_text_from_glm_response(result)
    if not text:
        raise ValueError("GLM-OCR 响应中找不到文本内容")
    echo = _parse_echo_from_text(text)
    if not echo.sub_stats:
        raise ValueError(
            f"GLM-OCR 识图后未能解析出副词条。原始文本前 300 字: {text[:300]}"
        )
    return echo
