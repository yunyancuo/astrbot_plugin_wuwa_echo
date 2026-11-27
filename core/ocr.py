"""多模态 OCR 调用、JSON 容错提取与声骸副词条清洗校验。"""

from __future__ import annotations

import asyncio
import json
import math
import re
from typing import Any

from .models import Echo, EchoSet, SubStat

SUBSTAT_RULES_ZH = """# 主词条 vs 副词条的区分(极其重要!)

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

# 副词条标准名(只能从这 13 个里选)

暴击, 暴击伤害, 攻击%, 生命%, 防御%, 共鸣效率, 普攻伤害加成, 重击伤害加成, 共鸣技能伤害加成, 共鸣解放伤害加成, 攻击, 生命, 防御
"""

SINGLE_ECHO_PROMPT_ZH = f"""你是鸣潮(Wuthering Waves)声骸识别助手。仔细识别图片中的声骸信息,严格按 JSON 输出,不要任何额外文字。

{SUBSTAT_RULES_ZH}
# 输出字段

- cost: 整数,COST 值(1/3/4)
- set_name: 字符串,合鸣效果套装名(如"凝夜白霜""熔山裂谷"). 识别不到填 null
- main_stat: 字符串,**可变那个主词条**的名字(COST 4 时不是固定的"攻击 150")
- main_stat_value: 数字,主词条数值
- level: 整数,强化等级(0-25,通常 +25)
- sub_stats: 数组(1-5 项),每项 {{"name": "副词条标准名", "value": 数字}}

仅输出 JSON,不要 markdown 代码块,不要任何解释:
{{"cost": 4, "set_name": "凝夜白霜", "main_stat": "暴击伤害", "main_stat_value": 44.0, "level": 25, "sub_stats": [{{"name": "暴击", "value": 9.3}}, {{"name": "攻击%", "value": 10.5}}]}}
"""

SET_ECHO_PROMPT_ZH = f"""你是鸣潮(Wuthering Waves)声骸识别助手。图片包含一套(最多 5 件)声骸,请逐个识别,严格按 JSON 输出,不要任何额外文字。

{SUBSTAT_RULES_ZH}
# 输出格式

{{"echoes": [
  {{"cost": 4, "set_name": "凝夜白霜", "main_stat": "暴击伤害", "main_stat_value": 44.0, "level": 25, "sub_stats": [{{"name": "暴击", "value": 9.3}}]}},
  ...
]}}

百分比类型只填数字部分(如 9.3 表示 9.3%)。识别不到的字段填 null。看不清的声骸直接跳过,不要编造数值。
"""

# 副词条数值上限(超过这个值说明 OCR 误把主词条当副词条了，应丢弃)
SUBSTAT_MAX_VALUE: dict[str, float] = {
    "暴击": 10.5,
    "暴击伤害": 21.0,
    "攻击%": 11.6,
    "生命%": 11.6,
    "防御%": 14.7,
    "共鸣效率": 11.6,
    "普攻伤害加成": 11.6,
    "重击伤害加成": 11.6,
    "共鸣技能伤害加成": 11.6,
    "共鸣解放伤害加成": 11.6,
    "攻击": 60.0,
    "生命": 580.0,
    "防御": 70.0,
}

# 多模态 OCR 高频输出词条变体 -> 插件标准副词条名
SUBSTAT_ALIASES: dict[str, str] = {
    "暴击率": "暴击",
    "暴击%": "暴击",
    "暴击率%": "暴击",
    "爆击": "暴击",
    "爆击率": "暴击",
    "暴伤": "暴击伤害",
    "爆伤": "暴击伤害",
    "暴击伤害%": "暴击伤害",
    "爆击伤害": "暴击伤害",
    "大攻击": "攻击%",
    "攻击力%": "攻击%",
    "百分比攻击": "攻击%",
    "攻击加成": "攻击%",
    "小攻击": "攻击",
    "攻击力": "攻击",
    "固定攻击": "攻击",
    "大生命": "生命%",
    "生命值%": "生命%",
    "百分比生命": "生命%",
    "生命加成": "生命%",
    "小生命": "生命",
    "生命值": "生命",
    "固定生命": "生命",
    "大防御": "防御%",
    "防御力%": "防御%",
    "百分比防御": "防御%",
    "防御加成": "防御%",
    "小防御": "防御",
    "防御力": "防御",
    "固定防御": "防御",
    "共鸣效率%": "共鸣效率",
    "充能": "共鸣效率",
    "充能效率": "共鸣效率",
    "普攻伤害": "普攻伤害加成",
    "普攻加成": "普攻伤害加成",
    "普攻伤害加成%": "普攻伤害加成",
    "重击伤害": "重击伤害加成",
    "重击加成": "重击伤害加成",
    "重击伤害加成%": "重击伤害加成",
    "技能伤害加成": "共鸣技能伤害加成",
    "共鸣技能伤害": "共鸣技能伤害加成",
    "共鸣技能加成": "共鸣技能伤害加成",
    "共鸣技能伤害加成%": "共鸣技能伤害加成",
    "解放伤害加成": "共鸣解放伤害加成",
    "共鸣解放伤害": "共鸣解放伤害加成",
    "共鸣解放加成": "共鸣解放伤害加成",
    "共鸣解放伤害加成%": "共鸣解放伤害加成",
    "大招伤害加成": "共鸣解放伤害加成",
}

_FLAT_TO_PERCENT_STAT = {
    "攻击": "攻击%",
    "生命": "生命%",
    "防御": "防御%",
}

VALID_COSTS = frozenset({1, 3, 4})
MAX_SUBSTATS = 5
MAX_SET_ECHOES = 5
MAX_RAW_TEXT_LEN = 200_000
MAX_IMAGE_URL_LEN = 4096

_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-+/=]+"),
    re.compile(r"(?i)(sk-[A-Za-z0-9_\-]{8,})"),
    re.compile(r"(?i)([?&](?:api[_-]?key|key|token|sig|signature|secret|auth)=)[^&\s]+"),
)


def redact_sensitive(text: str) -> str:
    """对日志或错误消息中的 API Key、Bearer Token 与 URL 鉴权参数脱敏。"""
    if not text:
        return ""
    out = str(text)
    out = _SECRET_PATTERNS[0].sub(r"\1***", out)
    out = _SECRET_PATTERNS[1].sub("sk-***", out)
    out = _SECRET_PATTERNS[2].sub(r"\1***", out)
    return out


def _to_int(value: Any, default: int = 0) -> int:
    """将 OCR 返回的数字（可能是 None / 带加号字符串 / 浮点）安全转换为整数。"""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, str):
        value = value.strip().lstrip("+")
        if not value:
            return default
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(num):
        return default
    return int(num)


def _parse_numeric(raw_value: Any) -> tuple[float | None, bool]:
    """解析 OCR 返回的数值，兼容 '+9.3%'、'10.5 %'、'40' 等格式。

    返回 (有限浮点值或 None, 原始表示是否带百分号)。
    """
    if raw_value is None or isinstance(raw_value, bool):
        return None, False

    has_percent = False
    if isinstance(raw_value, str):
        s = raw_value.strip().replace("％", "%")
        if not s:
            return None, False
        if s.endswith("%"):
            has_percent = True
            s = s[:-1].strip()
        s = s.lstrip("+").strip()
        if not s:
            return None, has_percent
        try:
            num = float(s)
        except ValueError:
            return None, has_percent
    elif isinstance(raw_value, (int, float)):
        num = float(raw_value)
    else:
        return None, False

    if not math.isfinite(num):
        return None, has_percent
    return num, has_percent


def _normalize_substat_name(raw_name: Any, *, has_percent: bool = False) -> str | None:
    """将 OCR 识别的副词条名清洗并映射到 13 个标准副词条名之一。"""
    if not isinstance(raw_name, str):
        return None
    cleaned = raw_name.strip().lstrip("+＋").strip()
    cleaned = cleaned.replace("％", "%")
    cleaned = re.sub(r"\s+", "", cleaned)
    if not cleaned:
        return None

    if cleaned in SUBSTAT_MAX_VALUE:
        std = cleaned
    else:
        std = SUBSTAT_ALIASES.get(cleaned)
        if std is None:
            return None

    # 容错纠偏：OCR 把 {"name": "攻击", "value": "9.4%"} 拆开写时自动转成 "攻击%"
    if has_percent and std in _FLAT_TO_PERCENT_STAT:
        std = _FLAT_TO_PERCENT_STAT[std]
    return std


def _clean_optional_str(value: Any) -> str | None:
    """安全清洗可选字符串字段（空串或非字符串转为 None）。"""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    return cleaned or None


def _extract_first_json_object(text: str) -> str | None:
    """使用标准 JSONDecoder 从文本中精准提取第一个合法 JSON 对象（dict）。"""
    decoder = json.JSONDecoder()
    idx = 0
    length = len(text)
    while idx < length:
        brace_pos = text.find("{", idx)
        if brace_pos < 0:
            return None
        try:
            obj, end_pos = decoder.raw_decode(text, brace_pos)
        except json.JSONDecodeError:
            idx = brace_pos + 1
            continue
        if isinstance(obj, dict):
            return text[brace_pos:end_pos]
        idx = end_pos
    return None


def _extract_json(text: str) -> str:
    """从视觉模型输出中提取第一个合法 JSON 对象。"""
    if not isinstance(text, str):
        raise ValueError("识别结果为空,请重新发送一次试试。")
    if len(text) > MAX_RAW_TEXT_LEN:
        raise ValueError("识别结果过长,请重新发送清晰截图后再试。")

    stripped = text.strip()
    if not stripped:
        raise ValueError("识别结果为空,请重新发送一次试试。")

    # 优先尝试 markdown 代码块内的 JSON 对象
    for fence_match in _CODE_FENCE_RE.finditer(stripped):
        candidate = _extract_first_json_object(fence_match.group(1))
        if candidate is not None:
            return candidate

    candidate = _extract_first_json_object(stripped)
    if candidate is not None:
        return candidate

    raise ValueError("识别结果中未找到有效 JSON 数据,请重新发送一次试试。")


def _load_json(raw_text: str) -> dict:
    """从原始识别文本中解析 JSON 字典，失败抛出面向用户的中文 ValueError。"""
    json_str = _extract_json(raw_text)
    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError("识别结果解析失败,请重新发送一次试试。") from e
    if not isinstance(data, dict):
        raise ValueError("识别结果格式不对,请重新发送一次试试。")
    return data


def _sanitize_substats(raw_subs: Any) -> list[SubStat]:
    """从 OCR 原始输出中筛出合法、去重后的副词条列表。

    清洗规则:
      - 映射常见 OCR 词条别称并兼容带 '%' / '+' 的数值字符串
      - 拦截 NaN / Inf / bool / <= 0 的非法数值
      - 超过该词条上限 1.05 倍直接丢弃（过滤 COST 4 固定主词条 "攻击 150" 误识）
      - 对重复识别出的同名副词条去重（保留首次出现的合法项）
    """
    if not isinstance(raw_subs, list):
        return []
    out: list[SubStat] = []
    seen_names: set[str] = set()
    for s in raw_subs:
        if not isinstance(s, dict):
            continue
        value, has_percent = _parse_numeric(s.get("value"))
        if value is None or value <= 0:
            continue
        name = _normalize_substat_name(s.get("name"), has_percent=has_percent)
        if not name or name not in SUBSTAT_MAX_VALUE:
            continue
        if value > SUBSTAT_MAX_VALUE[name] * 1.05:
            # 留 5% 容差防止浮点边缘，超出则判定为误识主词条并丢弃
            continue
        if name in seen_names:
            continue
        seen_names.add(name)
        out.append(SubStat(name=name, value=value))
    return out


def _validate_echo(echo: Echo, *, idx: int = -1) -> None:
    """校验声骸完整性：+25 声骸应有 1 个可变主词条（COST 3/4）与 5 条副词条。"""
    label = f"第 {idx + 1} 件" if idx >= 0 else ""
    if len(echo.sub_stats) < MAX_SUBSTATS:
        raise ValueError(
            f"{label}识别到 {len(echo.sub_stats)} 条副词条,不足 5 条。"
            "请发送清晰的完整声骸截图(+25 声骸应有 5 条副词条)。"
        )
    if len(echo.sub_stats) > MAX_SUBSTATS:
        raise ValueError(
            f"{label}识别到 {len(echo.sub_stats)} 条副词条,超过上限 5 条。"
            "请发送清晰的完整声骸截图(+25 声骸应有 5 条副词条)。"
        )
    if echo.cost in (3, 4) and not echo.main_stat:
        raise ValueError(
            f"{label}没识别出 COST {echo.cost} 声骸的主词条。"
            "图片可能模糊,请发送清晰的截图。"
        )


def _build_echo(item: dict, *, idx: int = -1) -> Echo:
    """将单件原始 OCR 字典构造并校验为 Echo 对象。"""
    subs = _sanitize_substats(item.get("sub_stats", []))
    cost = _to_int(item.get("cost"), 4)
    if cost not in VALID_COSTS:
        cost = 4
    level = max(0, min(_to_int(item.get("level"), 0), 25))
    main_stat_value, _ = _parse_numeric(item.get("main_stat_value"))

    echo = Echo(
        cost=cost,
        set_name=_clean_optional_str(item.get("set_name")),
        main_stat=_clean_optional_str(item.get("main_stat")),
        main_stat_value=main_stat_value,
        sub_stats=subs,
        level=level,
    )
    _validate_echo(echo, idx=idx)
    return echo


def parse_single(raw_text: str) -> Echo:
    """解析单件声骸的视觉 OCR 输出。"""
    data = _load_json(raw_text)
    return _build_echo(data)


def parse_set(raw_text: str) -> EchoSet:
    """解析整套声骸输出，采用宽容策略：单件失败只跳过该件，不整体报错。

    只要能识别出至少 1 件（最多 5 件）就返回；全部失败才抛出 ValueError。
    """
    data = _load_json(raw_text)
    echoes_raw = data.get("echoes", [])
    if not isinstance(echoes_raw, list):
        raise ValueError("识别结果格式不对,请重新发送一次试试。")

    echoes: list[Echo] = []
    failures: list[str] = []
    for i, item in enumerate(echoes_raw):
        if len(echoes) >= MAX_SET_ECHOES:
            break
        if not isinstance(item, dict):
            continue
        try:
            echoes.append(_build_echo(item, idx=i))
        except ValueError as e:
            failures.append(str(e))

    if not echoes:
        detail = f"主要问题: {failures[0]}" if failures else ""
        raise ValueError(
            f"这套声骸一件都没识别完整,图片可能模糊或截图不完整。{detail}"
        )
    return EchoSet(echoes=echoes)


async def vision_recognize(
    provider: Any, image_url: str, prompt: str, timeout: float = 45.0
) -> str:
    """调用 AstrBot LLM provider 的多模态接口识别图片，超时抛 asyncio.TimeoutError。"""
    if not isinstance(image_url, str) or not image_url.strip():
        raise ValueError("未获取到有效图片地址,请重新发送声骸截图。")
    cleaned_url = image_url.strip()
    if len(cleaned_url) > MAX_IMAGE_URL_LEN:
        raise ValueError("图片地址异常过长,请重新发送声骸截图。")

    coro = provider.text_chat(
        prompt=prompt,
        image_urls=[cleaned_url],
    )
    if timeout and timeout > 0:
        coro = asyncio.wait_for(coro, timeout=timeout)
    response = await coro

    if response is None:
        raise ValueError("视觉模型未返回识别结果,请稍后再试。")
    completion = getattr(response, "completion_text", None)
    if isinstance(completion, str) and completion.strip():
        return completion
    if isinstance(response, str) and response.strip():
        return response
    raise ValueError("视觉模型返回了空内容,请稍后再试。")
