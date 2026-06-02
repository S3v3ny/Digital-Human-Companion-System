"""
心理危机预警模块：两层检测 + 分级响应。

第一层：关键词粗筛（零延迟，无 API 调用）
第二层：LLM 精判（仅关键词命中时触发，最多 3s 超时）
状态机：SessionRiskState 跟踪单会话累积风险
日志：追加写 crisis_log.jsonl，供人工复查

风险等级：
  none   → 正常对话流程
  medium → 数字人切换关怀话术（caring_mode），不发前端弹窗
  high   → 完整预警：前端 crisis_alert 事件 + contactAction + 日志
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

# ── 第一层：高置信轻生关键词，已排除"累死了""气死我"等日常感叹 ──────────────
_CRISIS_KEYWORDS = [
    "不想活了", "不想活", "活不下去", "活着没意思", "活着有什么用",
    "想死", "去死", "死了算了", "死了算", "寻死", "轻生", "自杀",
    "结束生命", "结束自己", "了断", "消失算了", "消失就好了",
    "不如死", "不如死了", "死了比活着强",
    "跳楼", "上吊", "割腕", "服毒", "投河", "吞药",
    "人间不值得",
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
def keyword_pre_filter(text: str) -> bool:
    """命中任意关键词且不含排除短语则返回 True（零延迟）。"""
    compact = (text or "").replace(" ", "")
    if not any(kw in compact for kw in _CRISIS_KEYWORDS):
        return False
    if any(ex in compact for ex in _EXCLUSION_PHRASES):
        return False
    return True


# ── 第二层：LLM 精判 ──────────────────────────────────────────────────────────
async def classify_crisis_risk(user_text: str, context_messages: list) -> dict:
    """
    调用 LLM 精判风险等级。仅在关键词命中时调用。

    Returns:
        {"level": "none"|"medium"|"high", "score": float, "reason": str}
    """
    recent = (context_messages or [])[-4:]
    context_str = "\n".join(
        f"{'用户' if m['role'] == 'user' else '助手'}: {m['content'][:120]}"
        for m in recent
    )

    prompt = (
        "你是心理危机风险评估助手。请判断用户当前发言的自杀/轻生风险等级。\n\n"
        f"近期对话：\n{context_str or '（无历史）'}\n\n"
        f"用户最新发言：{user_text}\n\n"
        "判断标准：\n"
        "- none：日常抱怨或比喻，无真实轻生信号（如'累死了''气死我'）\n"
        "- medium：情绪低落、对生活失去意义感，但未明确表达自杀意图\n"
        "- high：明确表达想死/自杀的想法、意图或计划\n\n"
        "只输出一个 JSON 对象，格式：{\"level\": \"none|medium|high\", "
        "\"score\": 0.0-1.0, \"reason\": \"简短理由\"}"
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
        print("[crisis] LLM精判超时，回退关键词结果")
    except Exception as e:
        print(f"[crisis] LLM精判失败: {e}")

    # 超时或失败时，关键词命中按 medium 处理（保守策略）
    return {"level": "medium", "score": 0.5, "reason": "LLM不可用，关键词命中保守判定"}


# ── 会话风险状态机 ────────────────────────────────────────────────────────────
_ALERT_COOLDOWN = 1800  # 30 分钟后允许再次触发完整预警


class SessionRiskState:
    """跟踪单次 WebSocket 会话内的累积风险信号。"""

    def __init__(self):
        self.medium_count: int = 0
        self.high_count: int = 0
        self.last_alert_ts: float = 0.0

    def record(self, level: str) -> None:
        if level == "medium":
            self.medium_count += 1
        elif level == "high":
            self.high_count += 1

    @property
    def caring_mode(self) -> bool:
        """中等及以上风险：数字人切换关怀话术。"""
        return self.medium_count >= 1 or self.high_count >= 1

    @property
    def needs_full_alert(self) -> bool:
        """高风险单次，或中等风险累积 2 次；冷却 30 分钟后可再次触发。"""
        triggered = self.high_count >= 1 or self.medium_count >= 2
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
) -> None:
    """追加写 crisis_log.jsonl，每行一条 JSON 记录。"""
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "user_id": user_id or "unknown",
        "session_id": session_id or "unknown",
        "user_text": (user_text or "")[:200],
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
