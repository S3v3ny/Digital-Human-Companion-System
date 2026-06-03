"""
心理危机预警模块：三层检测 + 分级响应。

第一层A（direct_high）：具体方式词（跳楼/割腕/上吊等），无需 LLM，直接判 high
第一层B（hard）：直接轻生表达，超时 fallback=medium
第一层C（soft）：间接风险信号（无望感/负担感/道别），超时 fallback=none
第二层（本地）：TF-IDF 分类器（SOS-1K 训练），<5ms，高置信度结果直接采纳
第三层（LLM）：DeepSeek 精判，参照 C-SSRS + SOS-1K 少样本示例，最多 5s 超时
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

import json
import time
import aiohttp
import os
from pathlib import Path

from dotenv import load_dotenv
from crisis_classifier import predict as _local_predict

load_dotenv()
_API_KEY = os.getenv("API_KEY")
_LLM_URL = "https://api.deepseek.com/v1/chat/completions"

LOG_FILE = Path(__file__).resolve().parent / "crisis_log.jsonl"

# ── 第一层A-i：直接方式词（无需 LLM 精判，直接判 high） ──────────────────────
# 这些词描述具体的自杀方式，在本系统目标人群（老年陪伴场景）中几乎不存在歧义，
# 避免 LLM 超时导致降级漏报。排除词仅对 soft 信号生效，hard/direct_high 均不受影响。
_DIRECT_HIGH_KEYWORDS = [
    "跳楼", "割腕", "上吊", "服毒", "投河", "吞药",
]

# ── 第一层A-ii：高置信轻生关键词（直接表达），已排除"累死了""气死我"等日常感叹 ──
_CRISIS_KEYWORDS = [
    "不想活了", "不想活", "活不下去", "活着没意思", "活着有什么用",
    "想死", "去死", "死了算了", "死了算", "寻死", "轻生", "自杀",
    "结束生命", "结束自己", "了断", "消失算了", "消失就好了",
    "不如死", "不如死了", "死了比活着强",
    "人间不值得",
    # 补充遗漏的直接表达
    "想消失", "死了就好了", "死了大家都解脱", "结束一切",
    "永远睡过去", "不想再醒来", "死了一了百了",
    "一走了之", "一了百了", "了此残生", "了结自己", "活不下去了", "不想活着",
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
    # 老年人"去找/见/陪已故配偶"隐语（容忍"我/儿"中缀；语义歧义留给 LLM 结合上下文判）
    "陪老伴", "陪我老伴", "陪老伴儿", "陪我老伴儿",
    "找老伴", "找我老伴", "见老伴", "见我老伴",
    "去找我先生", "去找我太太", "去找我丈夫", "去找我妻子",
    # 隐晦跳跃表达（"轻轻一跳/只要一跳"暗指跳楼）
    "轻轻一跳", "只要一跳", "一跳就能见",
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
        "direct_high" - 具体自杀方式词（跳楼/割腕等），不等 LLM，直接判 high
        "hard"        - 直接表达轻生/自杀，超时 fallback 为 medium
        "soft"        - 间接信号（无望/负担/道别），超时 fallback 为 none（保守）
        ""            - 无命中，跳过 LLM 检测
    """
    compact = (text or "").replace(" ", "")
    has_exclusion = any(ex in compact for ex in _EXCLUSION_PHRASES)

    # 直接方式词 + 直接轻生意图：无排除词即判 direct_high（不走 LLM，避免精判超时
    # 漏报这类最明确的表达）。含排除词（听说/新闻/他说等第三方语境）则降级 hard 交 LLM。
    if (any(kw in compact for kw in _DIRECT_HIGH_KEYWORDS)
            or any(kw in compact for kw in _CRISIS_KEYWORDS)):
        return "hard" if has_exclusion else "direct_high"

    # soft 信号检查排除词，避免"听说有人撑不下去"等第三方讨论误触发
    if has_exclusion:
        return ""
    if any(kw in compact for kw in _SOFT_KEYWORDS):
        return "soft"
    return ""


# ── 宽风险语境词表：用于门控分类器兜底（非直接判定）─────────────────────────
# 这些词本身不足以判定危机，但出现时值得让分类器+LLM 进一步审查，
# 用以捕捉精确关键词漏判的「隐晦/迂回」高危表达（撞车、安排后事、留遗产等）。
# 刻意不含"保险/走了/离开"等高频歧义词，避免日常闲聊被误触发。
_RISK_CONTEXT_TERMS = [
    # 迂回的死亡方式
    "走到马路", "马路中间", "马路上", "撞车", "车祸", "出车祸", "出意外", "出点意外",
    "安眠药", "烧炭", "农药", "跳下", "从楼上", "从高处",
    # 临终 / 安排后事
    "临终", "后事", "遗书", "遗嘱", "遗产", "遗物", "存折",
    "整理好了", "都安排好了", "交代", "留给孩子", "留给儿子", "留给女儿",
    "给儿子留", "给孩子留", "给女儿留", "留笔钱", "留一笔", "赔偿金",
    # 绝望 / 告别隐语
    "解脱", "一了百了", "永别", "离开这个世界", "离开人世", "不在人世", "离开人间",
]

# 分类器兜底阈值：风险语境词命中后，分类器 high 概率达此值即升级 LLM 精判。
# 设 0.38：风险语境词已大幅缩小范围，此处宁可多送 LLM 也不漏判隐晦高危。
_BACKSTOP_HIGH_PROBA = 0.38


def detect_signal(text: str) -> str:
    """
    危机信号检测：关键词粗筛 + （风险语境词门控的）本地分类器兜底。

    1. 关键词命中优先（direct_high/hard/soft）。
    2. 未命中时，仅当文本含「风险语境词」才启用分类器兜底，
       避免对日常闲聊（分布外输入）误触发。分类器确认 high 或不可用时升级
       为 "classifier" 信号，交 LLM 结合上下文做最终判定。

    Returns:
        "direct_high" | "hard" | "soft" | "classifier" | ""
    """
    sig = keyword_pre_filter(text)
    if sig:
        return sig
    # 风险语境词门控：无风险语境词的普通对话直接放行
    compact = (text or "").replace(" ", "")
    if not any(term in compact for term in _RISK_CONTEXT_TERMS):
        return ""
    # 有风险语境词 → 分类器确认（不可用时保守升级，交 LLM 兜底）
    local = _local_predict(text)
    if not local["available"] or local["probas"].get("high", 0.0) >= _BACKSTOP_HIGH_PROBA:
        return "classifier"
    return ""


# ── SOS-1K 少样本示例（供 LLM 精判校准用，来自真实标注数据）────────────────
_FEW_SHOT_EXAMPLES = (
    "【判断示例（来自真实数据，供参考）】\n"
    "- 「跳楼死得很难看 所以还是好好活着吧」→ none（提到方式但在劝阻自己，无真实意念）\n"
    "- 「不是我想熬夜，我到希望24小时都睡着，这样也就不痛苦了」→ none（绝望感，无自杀意念）\n"
    "- 「我真的很想哭一哭，但是哭有什么用呢？我也想选择离开这个世界」→ medium（被动死亡意念，无具体方式）\n"
    "- 「有没有人可以当我的树洞啊，我好想去死」→ medium（强烈意愿，但无计划）\n"
    "- 「写了很长很长的遗书，如果发出去了就真的死掉了。就是今天我特别想死。」→ high（已写遗书，有立即行动意图）\n"
)

# ── 第二+三层：本地分类器 + LLM 精判 ─────────────────────────────────────────
async def classify_crisis_risk(
    user_text: str, context_messages: list, signal_type: str = "hard"
) -> dict:
    """
    两步精判：先本地 TF-IDF（毫秒级），高置信度直接返回；其余走 LLM。

    Args:
        signal_type: "hard"（直接表达）或 "soft"（间接信号），影响 fallback 策略

    Returns:
        {"level": "none"|"medium"|"high", "score": float, "reason": str}
    """
    # ── 第二层：本地分类器初筛 ─────────────────────────────────────────────────
    local = _local_predict(user_text)
    if local["available"]:
        level, score = local["level"], local["score"]
        # ≥0.75 时准确率 95.7%（SOS-1K 测试集），足够直接采纳
        if level == "high" and score >= 0.75:
            return {"level": "high", "score": score, "reason": f"本地分类器高置信判定 ({score:.2f})"}
        # soft 信号 + 本地高置信 none：排除误报，避免无谓 LLM 调用
        if level == "none" and score >= 0.75 and signal_type == "soft":
            return {"level": "none", "score": score, "reason": f"本地分类器排除风险 ({score:.2f})"}
    # ── 第三层：LLM 精判（有对话历史，处理模糊/复杂语境）──────────────────────

    recent = (context_messages or [])[-6:]
    context_str = "\n".join(
        f"{'用户' if m['role'] == 'user' else '助手'}: {m['content'][:150]}"
        for m in recent
    )

    if signal_type == "hard":
        signal_hint = "【触发原因：用户使用了直接轻生/自杀相关表达】"
    elif signal_type == "classifier":
        signal_hint = "【触发原因：风险分类器在用户发言中检测到隐晦的潜在自杀风险（如撞车、安排后事等迂回表达），请结合上下文判断是否为真实风险】"
    else:
        signal_hint = "【触发原因：用户出现了间接风险信号（无望感/自觉是负担/道别等），需综合上下文判断】"

    # 仅在置信度 > 0.5 时才提供本地分类器参考，避免低置信度结果锚定 LLM 判断
    local_hint = ""
    if local["available"] and local["score"] > 0.5:
        local_hint = (
            f"\n【本地分类器参考（仅供辅助，请综合上下文独立判断）】\n"
            f"  预测：{local['level']}，置信度：{local['score']:.2f}\n"
        )

    prompt = (
        "你是心理危机风险评估助手，参照哥伦比亚自杀严重程度评定量表（C-SSRS）进行判断。\n\n"
        f"{signal_hint}{local_hint}\n"
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
        + _FEW_SHOT_EXAMPLES
        + "\n只输出一个 JSON 对象，格式：{\"level\": \"none|medium|high\", "
        "\"score\": 0.0-1.0, \"reason\": \"简短理由（≤30字）\"}"
    )

    data = {
        "model": "deepseek-v4-pro",
        "messages": [
            {"role": "system", "content": "你是严格的心理危机分类器，只输出JSON，不输出任何其他内容。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        # deepseek-v4-pro 为推理模型，思考占用 reasoning_content（约数百 token），
        # 须留足预算，否则 content（JSON）被截断 → 解析失败 → 永远走 fallback。
        "max_tokens": 1024,
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
                # 用括号计数法提取最外层 {} 对，避免 reason 字段含 } 时被截断
                start = content.find("{")
                if start != -1:
                    depth, end = 0, -1
                    for i, ch in enumerate(content[start:], start):
                        if ch == "{":
                            depth += 1
                        elif ch == "}":
                            depth -= 1
                            if depth == 0:
                                end = i
                                break
                    if end != -1:
                        result = json.loads(content[start:end + 1])
                        level = result.get("level", "none")
                        if level not in ("none", "medium", "high"):
                            level = "none"
                        return {
                            "level": level,
                            "score": float(result.get("score", 0.0)),
                            "reason": str(result.get("reason", "")),
                        }
    except Exception as e:
        # CancelledError（来自外层 wait_for）会穿透此 except（BaseException 子类），
        # 其余网络/解析错误（含 401）在此处理后落入下方 fallback
        print(f"[crisis] LLM精判失败: {e}")

    # LLM 不可用 / 响应无有效 JSON 时的兜底（务必返回 dict，否则调用方解包 None 崩溃）：
    # hard / classifier 保守判 medium（进关怀模式）；soft 降级 none（避免误报）
    if signal_type in ("hard", "classifier"):
        return {"level": "medium", "score": 0.5, "reason": "LLM不可用，风险信号保守判定"}
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
