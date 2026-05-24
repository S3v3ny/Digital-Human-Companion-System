"""
提醒功能 — 内存存储 + 本地正则时间解析。
重启 server 会丢失，按用户要求保持简单。

用法：
    from reminders import try_parse_reminder, add, list_active, pop_due, remove, format_when
"""
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional


@dataclass
class Reminder:
    id: str
    when_ts: float
    content: str
    fired: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "whenTs": self.when_ts,
            "whenStr": format_when(self.when_ts),
            "content": self.content,
            "fired": self.fired,
        }


_REMINDERS: list[Reminder] = []


# ── 时间解析 ──────────────────────────────────────────────────────────────────
_CN_DIGITS = {"零": 0, "一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}


def _cn_num(s: str) -> Optional[int]:
    """简单中文数字 → int，仅覆盖 1-59 这个量级（提醒场景够用）"""
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s in _CN_DIGITS:
        return _CN_DIGITS[s]
    # 十X / X十 / X十Y
    m = re.fullmatch(r"([一二两三四五六七八九]?)十([一二三四五六七八九]?)", s)
    if m:
        tens = _CN_DIGITS.get(m.group(1), 1) if m.group(1) else 1
        ones = _CN_DIGITS.get(m.group(2), 0) if m.group(2) else 0
        return tens * 10 + ones
    return None


def _parse_time(text: str) -> Optional[datetime]:
    """从中文文本提取目标时间，失败返回 None"""
    now = datetime.now()

    num_pat = r"(\d+|[一二两三四五六七八九十]+)"

    # 「X 秒/分钟/小时 后」
    for unit, td_key in (("秒", "seconds"), ("分钟?", "minutes"),
                         ("(?:小时|个小时|钟头)", "hours")):
        m = re.search(num_pat + r"\s*" + unit + r"后", text)
        if m:
            n = _cn_num(m.group(1))
            if n is not None:
                return now + timedelta(**{td_key: n})

    # 日期偏移
    day_offset = 0
    if "明天" in text or "明儿" in text:
        day_offset = 1
    elif "后天" in text:
        day_offset = 2

    # 时段
    period_offset = 0
    has_period = False
    if any(w in text for w in ("下午", "傍晚")):
        period_offset, has_period = 12, True
    elif any(w in text for w in ("晚上", "夜里", "夜晚")):
        period_offset, has_period = 12, True
    elif any(w in text for w in ("上午", "早上", "早晨", "清晨")):
        period_offset, has_period = 0, True
    elif "中午" in text:
        period_offset, has_period = 12, True

    # 「X点(半|Y分)?」/「X时(半|Y分)?」
    m = re.search(num_pat + r"\s*(?:点|时)\s*(半|" + num_pat + r"\s*分)?", text)
    if m:
        hour = _cn_num(m.group(1))
        minute_grp = m.group(2)
        if minute_grp == "半":
            minute = 30
        elif minute_grp:
            # 群 3 是 (半|NUM\s*分) 里 NUM 那个；正则用了 num_pat 子组捕获
            minute = _cn_num(m.group(3)) if m.group(3) else 0
        else:
            minute = 0
        if hour is None:
            return None
        # 处理中午12点 / 夜里12点 → 0点 这种特殊情况
        if has_period and 0 < hour < 12:
            hour += period_offset
        elif has_period and hour == 12 and period_offset == 0:
            hour = 0  # 上午12点 ≈ 0点
        target = (now + timedelta(days=day_offset)).replace(
            hour=hour, minute=minute or 0, second=0, microsecond=0
        )
        # 未明确"今天"且目标已过，自动顺延到明天
        if day_offset == 0 and target <= now:
            target += timedelta(days=1)
        return target

    return None


_TIME_NOISE = re.compile(
    r"(?:今天|明天|后天|明儿|现在)"
    r"|(?:上午|下午|晚上|早上|早晨|清晨|中午|傍晚|夜里|夜晚)"
    r"|(?:\d+|[一二两三四五六七八九十]+)\s*(?:秒钟?|分钟?|小时|个小时|钟头)后"
    r"|(?:\d+|[一二两三四五六七八九十]+)\s*(?:点|时)\s*(?:半|(?:\d+|[一二三四五六七八九十]+)\s*分)?"
)


def _extract_content(text: str) -> str:
    """从 "X提醒我Y" 抽取 Y，并清除残留的时间词"""
    m = re.search(r"提醒(?:我|一下|我一下)?[:：，,]?\s*(.+)", text)
    raw = m.group(1).strip() if m else text
    raw = re.sub(r"[。！？!?\.]+$", "", raw).strip()
    # 清掉句内残留的时间噪声（如"5秒后喝水" → "喝水"）
    cleaned = _TIME_NOISE.sub("", raw)
    cleaned = re.sub(r"\s+", "", cleaned).strip("，,。.的 ")
    return cleaned or raw or "您交代的事"


def try_parse_reminder(text: str) -> Optional[tuple[datetime, str]]:
    """成功返回 (when, content)，否则返回 None"""
    if "提醒" not in text:
        return None
    when = _parse_time(text)
    if not when:
        return None
    return when, _extract_content(text)


# ── 存储 / 调度 ──────────────────────────────────────────────────────────────
def add(when: datetime, content: str) -> Reminder:
    r = Reminder(id=uuid.uuid4().hex[:8], when_ts=when.timestamp(), content=content)
    _REMINDERS.append(r)
    return r


def list_active() -> list[Reminder]:
    """返回所有未触发的提醒，按时间升序"""
    return sorted([r for r in _REMINDERS if not r.fired], key=lambda r: r.when_ts)


def pop_due(now_ts: Optional[float] = None) -> list[Reminder]:
    """取出所有到期但未触发的提醒，标记为 fired 并返回"""
    if now_ts is None:
        now_ts = time.time()
    due = []
    for r in _REMINDERS:
        if not r.fired and r.when_ts <= now_ts:
            r.fired = True
            due.append(r)
    return due


def remove(reminder_id: str) -> bool:
    global _REMINDERS
    before = len(_REMINDERS)
    _REMINDERS = [r for r in _REMINDERS if r.id != reminder_id]
    return len(_REMINDERS) < before


def format_when(ts: float) -> str:
    """格式化为「今天 15:00」/「明天 07:00」/「12月25日 09:30」"""
    dt = datetime.fromtimestamp(ts)
    today = datetime.now().date()
    delta_days = (dt.date() - today).days
    if delta_days == 0:
        prefix = "今天"
    elif delta_days == 1:
        prefix = "明天"
    elif delta_days == 2:
        prefix = "后天"
    else:
        prefix = dt.strftime("%m月%d日")
    return f"{prefix} {dt.strftime('%H:%M')}"


def build_fire_text(content: str, user_name: str = "") -> str:
    """生成数字人到点播报话术"""
    addr = f"{user_name}，" if user_name else ""
    return f"{addr}时间到啦，您之前让我提醒您{content}，记得放在心上哦。"


def build_ack_text(when_str: str, content: str, user_name: str = "") -> str:
    """登记后回复用户的确认话术"""
    addr = f"好的{user_name}" if user_name else "好的"
    return f"{addr}，我记下啦。{when_str}会提醒您{content}。"
