import asyncio
import json
import aiohttp
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

API_KEY = os.getenv("API_KEY")
PROFILE_FILE = Path(__file__).resolve().parent / "user_profiles.json"

# 每 N 轮触发一次信息提取
EXTRACTION_INTERVAL = 3
# 每 M 轮重新生成摘要（M 是 EXTRACTION_INTERVAL 的倍数）
SUMMARY_REGEN_EVERY_N_EXTRACTIONS = 3

_profiles: dict = {}
_profile_lock = asyncio.Lock()

_EMPTY_PROFILE = {
    "demographics": {
        "age": None,
        "gender": None,
        "location": None,
    },
    "social": {
        "occupation": None,
        "marital_status": None,
        "children": [],
        "living_situation": None,
    },
    "health": {
        "conditions": [],
        "mobility": None,
    },
    "psychological": {
        "communication_style": None,
        "emotional_tendencies": [],
        "topics_of_interest": [],
        "sensitive_topics": [],
        "personality_traits": [],
    },
    "preferences": {
        "preferred_address": None,
        "talk_length": None,
        "humor_tolerance": None,
    },
    "inferred_facts": [],
    "summary": "",
    "last_updated": None,
    "total_turns": 0,
    "total_extractions": 0,
    "turns_since_last_extraction": 0,
}


def _load_from_disk() -> dict:
    if not PROFILE_FILE.exists():
        return {}
    try:
        with PROFILE_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"[Profile] 读取失败: {e}")
        return {}


def _save_to_disk():
    try:
        with PROFILE_FILE.open("w", encoding="utf-8") as f:
            json.dump(_profiles, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Profile] 写入失败: {e}")


def get_profile(user_id: str) -> dict:
    if not _profiles:
        _profiles.update(_load_from_disk())
    if user_id not in _profiles:
        _profiles[user_id] = deepcopy(_EMPTY_PROFILE)
    return _profiles[user_id]


async def set_preferred_name(user_id: str, name: str):
    if not name:
        return
    async with _profile_lock:
        profile = get_profile(user_id)
        if not profile["preferences"]["preferred_address"]:
            profile["preferences"]["preferred_address"] = name
            _save_to_disk()


def build_profile_context(user_id: str) -> str:
    """生成注入 system prompt 的画像块，无数据时返回空字符串。"""
    profile = get_profile(user_id)
    summary = (profile.get("summary") or "").strip()

    if not summary:
        # LLM 摘要尚未生成时，用已知硬信息构造简短描述
        parts = []
        addr = profile.get("preferences", {}).get("preferred_address")
        if addr:
            parts.append(f"希望被称为「{addr}」")
        demo = profile.get("demographics", {})
        if demo.get("age"):
            parts.append(f"年龄 {demo['age']}")
        if demo.get("gender"):
            parts.append(f"性别{demo['gender']}")
        soc = profile.get("social", {})
        if soc.get("occupation"):
            parts.append(f"职业：{soc['occupation']}")
        if soc.get("living_situation"):
            parts.append(soc["living_situation"])
        psy = profile.get("psychological", {})
        if psy.get("topics_of_interest"):
            parts.append(f"感兴趣：{'、'.join(psy['topics_of_interest'][:3])}")
        if not parts:
            return ""
        summary = "；".join(parts)

    return (
        "\n        # 用户画像（已知背景，在回复中自然考虑，勿照本宣科）\n"
        f"        {summary}\n"
    )


def _apply_operations(profile: dict, operations: list) -> dict:
    profile = deepcopy(profile)
    for item in operations:
        field_path = (item.get("field") or "").strip()
        op = (item.get("op") or "set").lower()
        value = item.get("value")
        if not field_path or value is None:
            continue
        parts = field_path.split(".")
        target = profile
        try:
            for part in parts[:-1]:
                if not isinstance(target.get(part), dict):
                    target[part] = {}
                target = target[part]
            last_key = parts[-1]
            if op == "set":
                target[last_key] = value
            elif op == "append":
                if not isinstance(target.get(last_key), list):
                    target[last_key] = []
                if value not in target[last_key]:
                    target[last_key].append(value)
            elif op == "remove":
                lst = target.get(last_key)
                if isinstance(lst, list) and value in lst:
                    lst.remove(value)
        except Exception as e:
            print(f"[Profile] 操作失败 {field_path}: {e}")
    return profile


_EXTRACTION_PROMPT = """你是一个静默的用户信息提取器，从对话中提取用户透露的个人事实。

【最新对话】
用户：{user_text}
助手：{assistant_text}

【当前已知画像】
{current_profile}

规则：
1. 只提取用户明确说出或强烈暗示的内容，禁止推测
2. 已存在于画像中的相同信息跳过（op 用 noop）
3. 信息有更新时用 set 覆盖，列表追加用 append
4. 无任何新信息时输出空数组 []

输出格式（JSON 数组）：
[
  {{
    "field": "字段路径",
    "op": "set | append | remove",
    "value": "中文值",
    "evidence": "用户原文片段"
  }}
]

可用字段路径（只能用这些）：
demographics.age            整数或描述，如 72 或 "七十多岁"
demographics.gender         "男" 或 "女"
demographics.location       城市或省份
social.occupation           如 "退休教师"
social.marital_status       "已婚" | "丧偶" | "离异" | "单身"
social.children             孩子描述（append），如 "儿子在上海工作"
social.living_situation     "独居" | "与子女同住" | "与老伴同住" 等
health.conditions           疾病或健康问题（append），如 "高血压"
health.mobility             行动能力描述
psychological.communication_style  说话风格，如 "喜欢讲道理" | "话少"
psychological.emotional_tendencies 情绪倾向（append），如 "容易焦虑"
psychological.topics_of_interest   兴趣话题（append），如 "历史"
psychological.sensitive_topics     敏感话题（append），如 "孤独感"
psychological.personality_traits   性格特点（append），如 "内向"
preferences.preferred_address      希望被如何称呼
preferences.talk_length            "简短" 或 "详细"
preferences.humor_tolerance        "喜欢" 或 "不喜欢"

只输出 JSON 数组，不要任何解释或 Markdown 标记。"""


_SUMMARY_PROMPT = """根据以下用户画像数据，生成一段简洁的中文用户背景描述，供 AI 助手在回复时了解用户情况。

要求：
- 2~4 句话，不超过 80 字
- 只包含已知的确切信息，不推测
- 第三人称描述（如"用户是一位……"）
- 直接输出描述文字，不加任何标签或 Markdown

用户画像：
{profile_json}"""


async def _llm_call(prompt: str, max_tokens: int = 512) -> str:
    url = "https://api.siliconflow.cn/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "deepseek-ai/DeepSeek-V3.2",
        "messages": [
            {"role": "system", "content": "你是严格的 JSON 助手，只输出 JSON，不输出任何其他内容。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "stream": False,
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=25) as resp:
                resp.raise_for_status()
                res = await resp.json()
                return res["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"[Profile] LLM 调用失败: {e}")
        return ""


async def _extract_and_apply(user_id: str, user_text: str, assistant_text: str):
    async with _profile_lock:
        profile = get_profile(user_id)
        snapshot = {
            k: v for k, v in profile.items()
            if k not in ("summary", "total_turns", "total_extractions",
                         "turns_since_last_extraction", "last_updated")
        }

    prompt = _EXTRACTION_PROMPT.format(
        user_text=user_text,
        assistant_text=assistant_text[:300],
        current_profile=json.dumps(snapshot, ensure_ascii=False, indent=2),
    )

    raw = await _llm_call(prompt, max_tokens=512)
    if not raw:
        return

    try:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            cleaned = "\n".join(lines[1:])
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3].strip()
        operations = json.loads(cleaned)
        if not isinstance(operations, list):
            return
        operations = [o for o in operations if o.get("op", "").lower() != "noop"]
        if not operations:
            return
    except Exception as e:
        print(f"[Profile] 解析提取结果失败: {e} | 原文: {raw[:150]}")
        return

    async with _profile_lock:
        profile = get_profile(user_id)
        updated = _apply_operations(profile, operations)
        updated["last_updated"] = datetime.now(timezone.utc).isoformat()
        updated["turns_since_last_extraction"] = 0
        updated["total_extractions"] = profile.get("total_extractions", 0) + 1
        _profiles[user_id] = updated
        _save_to_disk()
        print(f"[Profile] user={user_id} 更新 {len(operations)} 条 | "
              f"总提取次数: {updated['total_extractions']}")

    if updated["total_extractions"] % SUMMARY_REGEN_EVERY_N_EXTRACTIONS == 0:
        asyncio.create_task(_regen_summary(user_id))


async def _regen_summary(user_id: str):
    async with _profile_lock:
        profile = get_profile(user_id)
        snapshot = {
            k: v for k, v in profile.items()
            if k not in ("summary", "total_turns", "total_extractions",
                         "turns_since_last_extraction", "last_updated")
        }

    has_data = any([
        snapshot.get("demographics", {}).get("age"),
        snapshot.get("demographics", {}).get("gender"),
        snapshot.get("social", {}).get("occupation"),
        snapshot.get("social", {}).get("marital_status"),
        snapshot.get("psychological", {}).get("topics_of_interest"),
        snapshot.get("psychological", {}).get("personality_traits"),
        snapshot.get("health", {}).get("conditions"),
    ])
    if not has_data:
        return

    url = "https://api.siliconflow.cn/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "deepseek-ai/DeepSeek-V3.2",
        "messages": [
            {"role": "system", "content": "你是用户画像摘要助手，生成简洁的用户背景描述。"},
            {"role": "user", "content": _SUMMARY_PROMPT.format(
                profile_json=json.dumps(snapshot, ensure_ascii=False, indent=2)
            )},
        ],
        "temperature": 0.3,
        "max_tokens": 150,
        "stream": False,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=25) as resp:
                resp.raise_for_status()
                res = await resp.json()
                summary = res["choices"][0]["message"]["content"].strip()

        async with _profile_lock:
            if user_id in _profiles:
                _profiles[user_id]["summary"] = summary
                _save_to_disk()
                print(f"[Profile] user={user_id} 摘要更新: {summary[:60]}...")

    except Exception as e:
        print(f"[Profile] 摘要生成失败: {e}")


async def delete_profile(user_id: str):
    """从内存和磁盘删除指定用户的画像数据。"""
    async with _profile_lock:
        # 冷启动时 _profiles 可能为空，先从磁盘加载再检查
        if not _profiles:
            _profiles.update(_load_from_disk())
        if user_id in _profiles:
            del _profiles[user_id]
            _save_to_disk()
            print(f"[Profile] user={user_id} 画像已删除")


async def record_turn(user_id: str, user_text: str, assistant_text: str):
    """每轮对话结束后调用：更新计数，达到阈值则异步触发画像提取。"""
    async with _profile_lock:
        profile = get_profile(user_id)
        profile["total_turns"] = profile.get("total_turns", 0) + 1
        profile["turns_since_last_extraction"] = profile.get("turns_since_last_extraction", 0) + 1
        turns_since = profile["turns_since_last_extraction"]
        _save_to_disk()

    if turns_since >= EXTRACTION_INTERVAL:
        asyncio.create_task(_extract_and_apply(user_id, user_text, assistant_text))
