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
# 中文数字 → int（支持 零/两/十/百/千 组合，覆盖 0–9999）
_CN_DIGIT = {"零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "贰": 2, "两": 2, "俩": 2,
             "三": 3, "叁": 3, "四": 4, "肆": 4, "五": 5, "伍": 5, "六": 6, "陆": 6,
             "七": 7, "柒": 7, "八": 8, "捌": 8, "九": 9, "玖": 9}
_CN_UNIT = {"十": 10, "拾": 10, "百": 100, "佰": 100, "千": 1000, "仟": 1000}

# 通用数字（阿拉伯或中文），用于各处时间正则
NUM = r"(\d+|[零〇一二两俩三四五六七八九十百千壹贰叁肆伍陆柒捌玖拾佰仟]+)"


def _cn_num(s: str) -> Optional[int]:
    """中文/阿拉伯数字字符串 → int，失败返回 None。"""
    if not s:
        return None
    s = s.strip()
    if re.fullmatch(r"\d+", s):
        return int(s)
    total, section, number, seen = 0, 0, 0, False
    for ch in s:
        if ch in _CN_DIGIT:
            number = _CN_DIGIT[ch]
            seen = True
        elif ch in _CN_UNIT:
            unit = _CN_UNIT[ch]
            if number == 0:
                number = 1  # 「十」=10、「百」=100
            if unit >= 100:
                section = (section + number) * unit
            else:
                section += number * unit
            number = 0
            seen = True
        else:
            return None
    return (total + section + number) if seen else None


def _apply_period(text: str, hour: int) -> int:
    """根据时段词把 12 小时制换算成 24 小时制。"""
    if any(w in text for w in ("凌晨", "午夜", "半夜", "夜间")):
        return 0 if hour == 12 else hour
    if any(w in text for w in ("上午", "早上", "早晨", "清晨", "早间", "今早", "明早")):
        return 0 if hour == 12 else hour
    if "中午" in text or "正午" in text:
        return 12 if hour == 12 else (hour + 12 if hour < 12 else hour)
    if any(w in text for w in ("下午", "午后", "傍晚")):
        return hour + 12 if 1 <= hour < 12 else hour
    if any(w in text for w in ("晚上", "夜里", "夜晚", "今晚", "明晚", "晚间")):
        if hour == 12:
            return 0
        return hour + 12 if 1 <= hour < 12 else hour
    return hour


def _period_default_hour(text: str) -> int:
    """只给了时段没给具体钟点时的默认小时。"""
    if any(w in text for w in ("凌晨", "午夜", "半夜")):
        return 6
    if any(w in text for w in ("上午", "早上", "早晨", "清晨", "今早", "明早")):
        return 8
    if "中午" in text or "正午" in text:
        return 12
    if any(w in text for w in ("下午", "午后", "傍晚")):
        return 15
    if any(w in text for w in ("晚上", "夜里", "夜晚", "今晚", "明晚")):
        return 20
    return 9


def _parse_minute(rest: str) -> int:
    """解析「点」之后的分钟部分：半 / 一刻 / 整 / 钟 / 零五 / 过五分 / 30 / 30分 …"""
    mk = re.match(r"\s*(一|二|两|三|[123])\s*刻", rest)
    if mk:
        return {"一": 15, "1": 15, "二": 30, "两": 30, "2": 30, "三": 45, "3": 45}[mk.group(1)]
    if re.match(r"\s*半", rest):
        return 30
    if re.match(r"\s*整", rest):
        return 0
    mm = re.match(r"\s*(?:零|过|又)?\s*(\d{1,2}|[零〇一二两三四五六七八九十]+)\s*分?", rest)
    if mm:
        v = _cn_num(mm.group(1))
        if v is not None and 0 <= v <= 59:
            return v
    return 0


def _parse_clock(text: str):
    """提取「时:分」，返回 (hour, minute) 或 None。"""
    # 数字时钟 8:30 / 8：30
    m = re.search(r"(\d{1,2})\s*[:：]\s*(\d{2})", text)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
    else:
        m = re.search(NUM + r"\s*(?:点|时)", text)
        if not m:
            return None
        hour = _cn_num(m.group(1))
        if hour is None:
            return None
        minute = _parse_minute(text[m.end():])
    hour = _apply_period(text, hour)
    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
        return None
    return hour, minute


def _parse_date(text: str, now: datetime):
    """确定目标日期，返回 (datetime, kind)。kind: ''|'explicit'|'weekday'。"""
    if "大后天" in text:
        return now + timedelta(days=3), "explicit"
    if "后天" in text:
        return now + timedelta(days=2), "explicit"
    if any(w in text for w in ("明天", "明儿", "明日", "明早", "明晚")):
        return now + timedelta(days=1), "explicit"
    if any(w in text for w in ("今天", "今日", "今晚", "今早", "今晨", "本日")):
        return now, "explicit"

    m = re.search(NUM + r"\s*(?:天|日)\s*(?:之后|以后|后)", text)
    if m:
        n = _cn_num(m.group(1))
        if n is not None:
            return now + timedelta(days=n), "explicit"

    m = re.search(NUM + r"\s*个?\s*(?:周|星期|礼拜)\s*(?:之后|以后|后)", text)
    if m:
        n = _cn_num(m.group(1))
        if n is not None:
            return now + timedelta(weeks=n), "explicit"

    # 周X / 星期X / 礼拜X，含 下/下下/这/本 前缀
    m = re.search(r"(下下|下|这|本)?\s*(?:周|星期|礼拜)\s*([一二三四五六日天])", text)
    if m:
        wd_map = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
        target_wd = wd_map[m.group(2)]
        delta = (target_wd - now.weekday()) % 7
        prefix = m.group(1)
        if prefix == "下":
            delta += 7
        elif prefix == "下下":
            delta += 14
        return now + timedelta(days=delta), "weekday"

    return now, ""


def _parse_relative_delta(text: str) -> Optional[timedelta]:
    """「X小时Y分Z秒后」之类的相对时长（可组合、可含半/个半），需带 后/之后/以后。"""
    if not re.search(r"(?:之后|以后|后)", text):
        return None
    units = (
        (r"小时|个小时|钟头", 3600),
        (r"分钟|分", 60),
        (r"秒钟|秒", 1),
    )
    total, found = 0.0, False
    for unit_re, secs in units:
        # X个半小时（一个半小时 = 90 分钟）
        for mm in re.finditer(NUM + r"\s*个半\s*(?:" + unit_re + r")", text):
            n = _cn_num(mm.group(1))
            if n is not None:
                total += (n + 0.5) * secs
                found = True
        # 半(个)小时（用后顾排除「一个半小时」里的「半小时」，避免与上面的「个半」重复计数）
        if re.search(r"(?<!个)半\s*个?\s*(?:" + unit_re + r")", text):
            total += 0.5 * secs
            found = True
        # X小时
        for mm in re.finditer(NUM + r"\s*(?:" + unit_re + r")", text):
            n = _cn_num(mm.group(1))
            if n is not None:
                total += n * secs
                found = True
    if found and total > 0:
        return timedelta(seconds=round(total))
    return None


def _parse_time(text: str) -> Optional[datetime]:
    """从中文文本提取目标时间，失败返回 None。"""
    now = datetime.now()

    # 0) 模糊即时词
    if any(w in text for w in ("马上", "立刻", "立马", "立即", "这就")):
        return now + timedelta(seconds=60)
    if any(w in text for w in ("一会儿", "一会", "待会儿", "待会", "等会儿", "等会", "过会儿", "过一会")):
        return now + timedelta(minutes=10)

    # 1) 优先：有明确钟点 → 日期偏移 + 钟点（支持「三天后下午三点」「下周五8点」）
    clock = _parse_clock(text)
    if clock is not None:
        base, kind = _parse_date(text, now)
        target = base.replace(hour=clock[0], minute=clock[1], second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=7 if kind == "weekday" else 1)
        return target

    # 2) 相对时长（秒/分/小时/天/周，可组合）
    delta = _parse_relative_delta(text)
    if delta is not None:
        return now + delta

    # 3) 只有日期/时段词，没有具体钟点 → 用时段默认钟点
    base, kind = _parse_date(text, now)
    _PERIOD_WORDS = ("凌晨", "早上", "早晨", "清晨", "上午", "中午", "正午", "下午",
                     "午后", "傍晚", "晚上", "夜里", "夜晚", "午夜", "半夜", "今晚", "明晚")
    if kind or any(w in text for w in _PERIOD_WORDS):
        hour = _period_default_hour(text)
        target = base.replace(hour=hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=7 if kind == "weekday" else 1)
        return target

    return None


_TIME_NOISE = re.compile(
    r"(?:大后天|后天|明天|明儿|明日|明早|明晚|今天|今日|今晚|今早|今晨|现在|本日)"
    r"|(?:下下|下|这|本)?\s*(?:周|星期|礼拜)\s*[一二三四五六日天]"
    r"|(?:凌晨|午夜|半夜|夜间|上午|早上|早晨|清晨|早间|中午|正午|下午|午后|傍晚|晚上|夜里|夜晚|晚间)"
    r"|(?:马上|立刻|立马|立即|这就|一会儿|一会|待会儿|待会|等会儿|等会|过会儿|过一会)"
    r"|" + NUM + r"\s*(?:天|日|个?(?:周|星期|礼拜))\s*(?:之后|以后|后)"
    r"|" + NUM + r"\s*个半\s*(?:小时|个小时|钟头|分钟|分|秒钟|秒)\s*(?:之后|以后|后)?"
    r"|半\s*个?\s*(?:小时|钟头|分钟|分|秒钟|秒)\s*(?:之后|以后|后)?"
    r"|" + NUM + r"\s*(?:秒钟|秒|分钟|分|小时|个小时|钟头)\s*(?:之后|以后|后)"
    r"|(?:\d{1,2})\s*[:：]\s*\d{2}"
    r"|" + NUM + r"\s*(?:点|时)\s*(?:整|半|钟|(?:一|二|两|三|[123])\s*刻|(?:零|过|又)?\s*(?:\d{1,2}|[零〇一二两三四五六七八九十]+)\s*分?)?"
)


def _extract_content(text: str) -> str:
    """从 "X提醒我Y" 抽取 Y，并清除残留的时间词。"""
    m = re.search(r"提醒(?:我|一下|我一下|你)?[:：，,]?\s*(.+)", text)
    raw = m.group(1).strip() if m else text
    raw = re.sub(r"[。！？!?\.]+$", "", raw).strip()
    # 清掉句内残留的时间噪声（如"5秒后喝水" → "喝水"）
    cleaned = _TIME_NOISE.sub("", raw)
    cleaned = re.sub(r"\s+", "", cleaned).strip("，,。.、的了要给我在 ")
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
