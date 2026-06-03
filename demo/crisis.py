"""
心理危机预警模块：两层检测 + 分级响应。

第一层A（hard）：高置信直接表达关键词，不受排除词拦截，超时 fallback=medium
第一层B（soft）：间接风险信号（无望感/负担感/道别），受排除词拦截，超时 fallback=none
第二层：LLM 精判，参照 C-SSRS 维度，仅关键词命中时触发，最多 3s 超时
状态机：SessionRiskState 跟踪单会话累积风险，hard/soft medium 分源计数
日志：追加写 crisis_log.jsonl，含 signal_type 字段，供人工复查

风险等级：
  none   → 正常对话流程
  medium → 数字人切换关怀话术（caring_mode），10 分钟无新信号自动衰减
  high   → 完整预警：前端 crisis_alert 事件 + contactAction + 日志

完整预警触发条件（任一）：
  high_count >= 1，或 hard_medium_count >= 2，或 soft_medium_count >= 3
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import aiohttp
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
_API_KEY = os.getenv("API_KEY")
_LLM_URL = "https://api.siliconflow.cn/v1/chat/completions"

LOG_FILE = Path(__file__).resolve().parent / "crisis_log.jsonl"

# ── 第一层A：高置信轻生关键词（直接表达），已排除"累死了""气死我"等日常感叹 ──
_CRISIS_KEYWORDS = [
    "不想活了", "不想活", "活不下去", "活着没意思", "活着有什么用",
    "想死", "去死", "死了算了", "死了算", "寻死", "轻生", "自杀",
    "结束生命", "结束自己", "了断", "消失算了", "消失就好了",
    "不如死", "不如死了", "死了比活着强",
    "跳楼", "上吊", "割腕", "服毒", "投河", "吞药",
    "人间不值得",
    # 补充遗漏的直接表达
    "想消失", "死了就好了", "死了大家都解脱", "结束一切",
    "永远睡过去", "不想再醒来", "死了一了百了",
]

# ── 第一层B：间接风险信号（被动意念/无望感/负担感/道别），触发 LLM 精判 ────────
# 参照 C-SSRS 被动死亡意念、无望感、认知扭曲（自觉是负担）等维度
_SOFT_KEYWORDS = [
    # 无望感（去掉"好累好累""活着好累"等日常高频感叹，保留有明确绝望指向的短语）
    "看不到希望", "没有任何希望", "没有希望了", "感觉没有希望",
    "以后不会好了", "永远不会好", "永远好不了",
    "什么都没意义", "感觉没有意义", "活着没什么意义",
    "撑不下去了", "撑不住了", "不想撑了", "坚持不下去了",
    # 被动死亡意念（C-SSRS 第1级）
    "希望睡过去不醒", "睡过去就好了", "希望不要醒来",
    "要是我死了", "要是我不在了", "如果我不在了",
    "感觉活着好没意思",
    # 认知扭曲：自觉是负担（被研究确认为高风险独立因素）
    "我是累赘", "我是个累赘", "拖累了大家", "拖累家人", "拖累他们",
    "没有我会更好", "没有我大家更好", "没有我他们会更好",
    "我的存在是负担", "我的存在是累赘",
    # 道别信号（仅保留非日常语境的异常道别，移除"记住我""帮我照顾""跟你说再见了"等高频日常词）
    "最后想谢谢你", "和你说最后一次", "把东西都整理好了",
    # 老年群体特有间接表达（中文临床文献明确点名的隐喻性信号）
    "这辈子差不多了", "不想拖累孩子", "不想拖累儿女",
]

# 第三方讨论/新闻/文学语境排除词，避免"听说有人自杀"误触发
_EXCLUSION_PHRASES = [
    "新闻", "报道", "电影", "小说", "历史",
    "他说", "她说", "听说", "看到", "读到",
]

# 对外暴露的危机热线列表（推送给前端展示）
CRISIS_HOTLINES = [
    {"name": "全国统一心理援助热线", "phone": "12356", "note": "24小时"},
    {"name": "北京心理危机干预中心", "phone": "010-82951332", "note": "24小时"},
    {"name": "生命热线", "phone": "400-821-1215", "note": "24小时"},
    {"name": "中国心理危机与自杀干预中心", "phone": "010-62715275", "note": "24小时"},
]


# ── 第一层：关键词粗筛 ────────────────────────────────────────────────────────
def keyword_pre_filter(text: str) -> str:
    """
    命中关键词且不含排除短语则返回信号强度，否则返回空字符串。

    Returns:
        "hard"  - 直接表达轻生/自杀，超时 fallback 为 medium
        "soft"  - 间接信号（无望/负担/道别），超时 fallback 为 none（保守）
        ""      - 无命中，跳过 LLM 检测
    """
    compact = (text or "").replace(" ", "")
    # hard 信号不受排除词拦截——直接提及自杀方法/意图，交给 LLM 精判上下文
    if any(kw in compact for kw in _CRISIS_KEYWORDS):
        return "hard"
    # soft 信号才检查排除词，避免"听说有人撑不下去"等第三方讨论误触发
    if any(ex in compact for ex in _EXCLUSION_PHRASES):
        return ""
    if any(kw in compact for kw in _SOFT_KEYWORDS):
        return "soft"
    return ""


# ── 第二层：LLM 精判 ──────────────────────────────────────────────────────────
async def classify_crisis_risk(
    user_text: str, context_messages: list, signal_type: str = "hard"
) -> dict:
    """
    调用 LLM 精判风险等级。仅在关键词命中时调用。

    Args:
        signal_type: "hard"（直接表达）或 "soft"（间接信号），影响 prompt 侧重点

    Returns:
        {"level": "none"|"medium"|"high", "score": float, "reason": str}
    """
    recent = (context_messages or [])[-6:]
    context_str = "\n".join(
        f"{'用户' if m['role'] == 'user' else '助手'}: {m['content'][:150]}"
        for m in recent
    )

    signal_hint = (
        "【触发原因：用户使用了直接轻生/自杀相关表达】"
        if signal_type == "hard"
        else "【触发原因：用户出现了间接风险信号（无望感/自觉是负担/道别等），需综合上下文判断】"
    )

    prompt = (
        "你是心理危机风险评估助手，参照哥伦比亚自杀严重程度评定量表（C-SSRS）进行判断。\n\n"
        f"{signal_hint}\n\n"
        f"近期对话（最多6轮）：\n{context_str or '（无历史）'}\n\n"
        f"用户最新发言：{user_text}\n\n"
        "【判断标准（严格遵守）】\n"
        "- none：日常抱怨、比喻性表达、第三方讨论，综合上下文判断无真实风险\n"
        "  示例：'累死了''气死我''听说有人自杀'\n"
        "- medium（C-SSRS 被动意念/危险因素累积）：满足以下任一：\n"
        "  ① 表达无望感：感觉没有未来、活着没意义、以后不会好了\n"
        "  ② 被动死亡意念：希望死去或消失，但无具体方法或计划\n"
        "  ③ 自觉是负担：认为自己拖累他人、没有自己大家更好\n"
        "  ④ 情绪极度低落 + 明显社交退缩信号\n"
        "- high（C-SSRS 主动意念/有计划/行为意图）：满足以下任一：\n"
        "  ① 明确表达想死、想自杀、有具体方法（跳楼/割腕/服药等）\n"
        "  ② 表达道别、安排后事、赠送遗物的意图\n"
        "  ③ 表达立即行动的意图\n\n"
        "只输出一个 JSON 对象，格式：{\"level\": \"none|medium|high\", "
        "\"score\": 0.0-1.0, \"reason\": \"简短理由（≤30字）\"}"
    )

    data = {
        "model": "deepseek-ai/DeepSeek-V3.2",
        "messages": [
            {"role": "system", "content": "你是严格的心理危机分类器，只输出JSON，不输出任何其他内容。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 100,
        "stream": False,
    }

    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                _LLM_URL,
                headers={"Authorization": f"Bearer {_API_KEY}", "Content-Type": "application/json"},
                json=data,
            ) as resp:
                resp.raise_for_status()
                res_json = await resp.json()
                content = res_json["choices"][0]["message"]["content"].strip()
                m = re.search(r"\{.*?\}", content, re.DOTALL)
                if m:
                    result = json.loads(m.group())
                    level = result.get("level", "none")
                    if level not in ("none", "medium", "high"):
                        level = "none"
                    return {
                        "level": level,
                        "score": float(result.get("score", 0.0)),
                        "reason": str(result.get("reason", "")),
                    }
    except asyncio.TimeoutError:
        print(f"[crisis] LLM精判超时，signal_type={signal_type}")
    except Exception as e:
        print(f"[crisis] LLM精判失败: {e}")

    # 超时或失败时：hard 信号保守判 medium；soft 信号降级为 none（避免误报）
    if signal_type == "hard":
        return {"level": "medium", "score": 0.5, "reason": "LLM不可用，直接信号保守判定"}
    return {"level": "none", "score": 0.0, "reason": "LLM不可用，间接信号降级"}


# ── 会话风险状态机 ────────────────────────────────────────────────────────────
_ALERT_COOLDOWN = 1800   # 30 分钟后允许再次触发完整预警
_MEDIUM_DECAY  = 600     # medium 信号 10 分钟无新信号后自动退出 caring_mode


class SessionRiskState:
    """跟踪单次 WebSocket 会话内的累积风险信号。"""

    def __init__(self):
        self.hard_medium_count: int = 0   # hard 信号 → LLM 判 medium
        self.soft_medium_count: int = 0   # soft 信号 → LLM 判 medium
        self.high_count: int = 0
        self.last_alert_ts: float = 0.0
        self.last_medium_ts: float = 0.0

    def record(self, level: str, signal_type: str = "hard") -> None:
        if level == "medium":
            if signal_type == "soft":
                self.soft_medium_count += 1
            else:
                self.hard_medium_count += 1
            self.last_medium_ts = time.time()
        elif level == "high":
            self.high_count += 1

    @property
    def medium_count(self) -> int:
        """兼容外部读取（日志等），返回总 medium 计数。"""
        return self.hard_medium_count + self.soft_medium_count

    @property
    def caring_mode(self) -> bool:
        """
        中等及以上风险切换关怀话术。
        high 信号持续有效；medium 信号 10 分钟无新信号后衰减退出。
        """
        if self.high_count >= 1:
            return True
        if self.medium_count >= 1:
            return (time.time() - self.last_medium_ts) < _MEDIUM_DECAY
        return False

    @property
    def needs_full_alert(self) -> bool:
        """
        触发条件（任一）：
        - high_count >= 1（直接表达，立即预警）
        - hard_medium_count >= 2（两次硬关键词后 LLM 判 medium）
        - soft_medium_count >= 3（三次间接信号均判 medium）
        冷却 30 分钟后可再次触发。
        """
        triggered = (
            self.high_count >= 1
            or self.hard_medium_count >= 2
            or self.soft_medium_count >= 3
        )
        cooled = (time.time() - self.last_alert_ts) >= _ALERT_COOLDOWN
        return triggered and cooled

    def mark_alerted(self) -> None:
        self.last_alert_ts = time.time()


# ── 危机预警播报文本 ──────────────────────────────────────────────────────────
def build_crisis_alert_reply() -> str:
    """生成数字人在 crisis_alert 触发时主动播报的文本：关怀句 + 热线号码。"""
    primary = CRISIS_HOTLINES[0]
    lines = [
        f"我一直在您身边，您不是一个人。",
        f"您也可以随时拨打{primary['name']} {primary['phone']}，{primary['note']}都有专业的人在等待接听。",
    ]
    return "".join(lines)


# ── 审计日志 ──────────────────────────────────────────────────────────────────
def log_crisis_event(
    user_id: str,
    session_id: str,
    user_text: str,
    level: str,
    score: float,
    reason: str,
    action: str,
    signal_type: str = "",
) -> None:
    """追加写 crisis_log.jsonl，每行一条 JSON 记录。"""
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "user_id": user_id or "unknown",
        "session_id": session_id or "unknown",
        "user_text": (user_text or "")[:200],
        "signal_type": signal_type,
        "level": level,
        "score": round(score, 3),
        "reason": reason,
        "action": action,
    }
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[crisis] 日志写入失败: {e}")
