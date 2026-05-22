"""
后端工具：仅保留天气 MCP（实时数据）+ 日历（本地计算）。

依赖：pip install mcp

天气走远程 MCP server（modelscope 托管，SSE 传输），日历用本地 datetime。
其他场景（讲笑话/听故事/养生/听歌/解闷等）由 LLM 用自身知识回答，不再走任何外部工具。

.env 可选覆盖：
  WEATHER_MCP_URL=...   DEFAULT_CITY=...
"""

import os
import re
import json
import asyncio
import datetime
import traceback
from typing import Optional, Dict, Any, List

from dotenv import load_dotenv

load_dotenv()

WEATHER_MCP_URL = os.getenv(
    "WEATHER_MCP_URL",
    "https://mcp.api-inference.modelscope.net/fd58fbe7964240/sse",
)
DEFAULT_CITY = os.getenv("DEFAULT_CITY", "北京")

# --- MCP SDK 软依赖 ---
try:
    from mcp import ClientSession
    from mcp.client.sse import sse_client
    _MCP_OK = True
except ImportError:
    _MCP_OK = False
    print("[tools] 未检测到 mcp 包，运行: pip install mcp")

# 工具名缓存（避免每次都 list_tools）
_weather_tools_cache: Optional[List[str]] = None


# =============================================================
#                       MCP 底层调用辅助
# =============================================================
def _extract_text(mcp_result) -> str:
    """从 CallToolResult.content 中提取所有 TextContent 文本。"""
    parts = []
    for item in (mcp_result.content or []):
        text = getattr(item, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


_ERROR_HINTS = (
    "http error", "httperror", "http错误",
    "请求失败", "调用失败", "查询失败",
    "请重试", "稍后重试",
    "exception", "traceback",
    "rate limit", "quota",
    "404", "400", "500", "502", "503",
)


def _looks_like_error(text: str) -> bool:
    """简单启发式：判断 MCP 返回的文本是否实际上是一段错误消息。"""
    if not text:
        return True
    lower = text.lower()
    if any(h in lower for h in _ERROR_HINTS):
        return True
    if any(zh in text for zh in ("错误", "失败", "异常")):
        # 但同时含天气字段就当真实数据
        if any(ok in text for ok in ("气温", "温度", "湿度", "风", "天气", "temp", "humidity")):
            return False
        return True
    return False


async def _list_tools_sse(url: str) -> List[str]:
    async with sse_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            return [t.name for t in result.tools]


async def _call_sse(url: str, tool_name: str, args: dict, timeout: float = 25):
    async with sse_client(url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await asyncio.wait_for(
                session.call_tool(tool_name, arguments=args),
                timeout=timeout,
            )


# =============================================================
#                       天气（MCP SSE）
# =============================================================
async def get_weather(city: Optional[str] = None) -> Dict[str, Any]:
    """返回 dict：ok / city / temp / text / wind_* / humidity / raw_text / error"""
    global _weather_tools_cache
    if not _MCP_OK:
        return {"ok": False, "error": "未安装 mcp 包，请 pip install mcp"}

    city = (city or DEFAULT_CITY).strip()
    print(f"[MCP weather] 请求 city='{city}'")

    try:
        if _weather_tools_cache is None:
            try:
                _weather_tools_cache = await asyncio.wait_for(
                    _list_tools_sse(WEATHER_MCP_URL), timeout=10
                )
                print(f"[MCP weather] 可用工具: {_weather_tools_cache}")
            except Exception as e:
                traceback.print_exc()
                return {"ok": False, "error": f"无法连接天气 MCP（{type(e).__name__}: {e}）"}

        if not _weather_tools_cache:
            return {"ok": False, "error": "天气 MCP 没有可用工具"}

        tool = next(
            (n for n in _weather_tools_cache if "weather" in n.lower() or "天气" in n),
            _weather_tools_cache[0],
        )

        last_err = None
        for arg_key in ("city", "location", "city_name", "place", "query"):
            try:
                result = await _call_sse(WEATHER_MCP_URL, tool, {arg_key: city})
                text = _extract_text(result)
                if not text:
                    continue
                if _looks_like_error(text):
                    last_err = text[:200]
                    continue
                return _parse_weather_text(text, city)
            except Exception as e:
                last_err = e
                print(f"[MCP weather] arg={arg_key} 失败: {type(e).__name__}: {e}")
        return {"ok": False, "error": f"天气服务返回异常：{last_err}"}

    except asyncio.TimeoutError:
        return {"ok": False, "error": "天气 MCP 超时"}
    except Exception as e:
        traceback.print_exc()
        return {"ok": False, "error": f"天气 MCP 异常：{type(e).__name__}: {e}"}


def _parse_weather_text(text: str, fallback_city: str) -> Dict[str, Any]:
    """先按 JSON 解，失败则正则抽关键字段。"""
    try:
        d = json.loads(text)
        if isinstance(d, dict):
            temp = d.get("temp") or d.get("temperature") or d.get("气温")
            if isinstance(temp, (int, float)):
                temp = round(float(temp), 1)
            wind_speed = d.get("wind_speed") or d.get("windSpeed")
            return {
                "ok": True,
                "city":       d.get("city") or d.get("location") or d.get("地点") or fallback_city,
                "temp":       temp,
                "feels_like": d.get("feels_like") or d.get("feelsLike") or d.get("体感"),
                "text":       d.get("text") or d.get("weather") or d.get("description") or d.get("天气") or d.get("condition"),
                "wind_dir":   d.get("wind_dir") or d.get("windDir") or d.get("风向"),
                "wind_scale": d.get("wind_scale") or d.get("windScale") or d.get("风力"),
                "wind_speed": wind_speed,
                "humidity":   d.get("humidity") or d.get("湿度"),
                "raw_text":   text,
            }
    except (json.JSONDecodeError, TypeError):
        pass

    def grab(pat):
        m = re.search(pat, text)
        return m.group(1).strip() if m else None

    return {
        "ok": True,
        "city":       grab(r"(?:城市|地点|location)[：:]\s*([^\s,，]+)") or fallback_city,
        "temp":       grab(r"(?:气温|温度|temp(?:erature)?)[：:]\s*(-?\d+(?:\.\d+)?)") or grab(r"(-?\d{1,2})\s*[°℃]"),
        "feels_like": grab(r"(?:体感|feels?\s*like)[：:]\s*(-?\d+(?:\.\d+)?)"),
        "text":       grab(r"(?:天气|状况|weather)[：:]\s*([^\s,，\n]+)"),
        "wind_dir":   grab(r"(?:风向|wind)[：:]\s*([^\s,，\n]+)"),
        "wind_scale": grab(r"(?:风力|风级|wind\s*scale)[：:]\s*(\d+)"),
        "humidity":   grab(r"(?:湿度|humidity)[：:]\s*(\d+)"),
        "raw_text":   text,
    }


def _temp_to_int(v):
    """温度统一四舍五入成整数，避免小数点让 TTS / 小模型读错。"""
    try:
        return int(round(float(v)))
    except (TypeError, ValueError):
        return None


def format_weather_for_llm(d: dict) -> str:
    """把天气数据格式化成一段干净的中文整句，让 LLM 尽量原文复述、避免再加工出错。"""
    if not d.get("ok"):
        return f"天气查询失败：{d.get('error')}"

    sentences = []
    city = d.get("city") or "本地"
    temp = _temp_to_int(d.get("temp"))
    # "阴，多云" → "阴转多云"，让 LLM 读着自然
    weather_text = (d.get("text") or "").replace("，", "转").replace(",", "转")

    # 第一句：城市 + 天气 + 气温（最关键的信息，确保最先念出）
    head = f"今天{city}天气{weather_text}" if weather_text else f"今天{city}"
    if temp is not None:
        head += f"，气温{temp}度"
    sentences.append(head + "。")

    # 第二句：风
    if d.get("wind_dir") and d.get("wind_scale"):
        sentences.append(f"风向{d['wind_dir']}，{d['wind_scale']}级风。")
    elif d.get("wind_speed") is not None:
        ws_int = _temp_to_int(d["wind_speed"])
        if ws_int is not None:
            sentences.append(f"风速大约{ws_int}米每秒。")

    # 第三句：湿度
    if d.get("humidity"):
        try:
            sentences.append(f"湿度{int(d['humidity'])}%。")
        except (TypeError, ValueError):
            pass

    if not sentences:
        return d.get("raw_text") or "天气数据为空"
    return "".join(sentences)


# =============================================================
#                       日历（本地，供 /api/calendar 用）
# =============================================================
_WEEKDAY_CN = ["一", "二", "三", "四", "五", "六", "日"]


def get_calendar() -> Dict[str, Any]:
    now = datetime.datetime.now()
    result = {
        "year": now.year,
        "month": now.month,
        "day": now.day,
        "weekday": f"星期{_WEEKDAY_CN[now.weekday()]}",
        "time": now.strftime("%H:%M"),
    }
    try:
        from zhdate import ZhDate
        result["lunar"] = ZhDate.from_datetime(now).chinese()
    except Exception:
        pass
    return result


# =============================================================
#                       关键词路由（仅天气）
# =============================================================
_KEYWORD_ROUTES = [
    ("weather", [r"天气", r"温度", r"下雨", r"出门冷", r"穿多少", r"几度"]),
]


def detect_tool(user_text: str) -> Optional[str]:
    text = (user_text or "").strip()
    if not text:
        return None
    for tool_name, patterns in _KEYWORD_ROUTES:
        for pat in patterns:
            if re.search(pat, text):
                return tool_name
    return None


# =============================================================
#                       统一调用入口
# =============================================================
async def call_tool(tool_name: str, user_text: str = "") -> str:
    """目前仅天气走真实查询，其它对话由 LLM 自己回答。"""
    try:
        if tool_name == "weather":
            data = await get_weather()
            body = format_weather_for_llm(data)
            return (
                "【实时天气】\n"
                f"{body}\n"
                "【念稿要求】请把上面那几句天气描述原文复述给用户，"
                "可以在最前面加一句温暖的称呼，最后加一句关心的话（如多穿点/带伞/慢慢走）。"
                "数字（气温、湿度、风速）必须和上面完全一致，不允许改动或重复读字。"
                "不要自己加体感温度、空气质量、未来几天等没出现的内容。"
            )
    except Exception as e:
        return f"工具调用失败：{e}"
    return ""
