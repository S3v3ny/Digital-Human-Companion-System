import aiohttp
import asyncio
import json
from pathlib import Path
# --- 【1. 新增 RAG 依赖】 ---
import chromadb
from sentence_transformers import SentenceTransformer
# ---------------------------

from dotenv import load_dotenv
import os

load_dotenv()  # 这一行必须在 os.getenv 之前执行
API_KEY = os.getenv("API_KEY")

from tools import detect_tool, call_tool


MEMORY_FILE = Path(__file__).resolve().parent / "session_memory.json"
MAX_HISTORY_MESSAGES = 6

# --- 【2. 全局加载知识库组件】 ---
print("正在加载 RAG 检索模块...")
# 加载向量模型
_BGE_LOCAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "bge-small-zh-v1.5")
embedder = SentenceTransformer(_BGE_LOCAL if os.path.isdir(_BGE_LOCAL) else 'BAAI/bge-small-zh-v1.5')
# 连接本地数据库 (注意：确保启动 server.py 时的终端路径在 demo 文件夹下)
chroma_client = chromadb.PersistentClient(path="./vector_db")
try:
    psy_collection = chroma_client.get_collection(name="psy_cbt_knowledge")
    print(f"[RAG] 知识库已加载，共 {psy_collection.count()} 条数据")
except Exception:
    psy_collection = chroma_client.get_or_create_collection(name="psy_cbt_knowledge")
    print("[RAG] 知识库为空，RAG 功能不可用。请先运行 build_kb.py 导入数据。")
# --------------------------------

INTERRUPT_COMMAND_HINTS = [
    "停一下", "先停", "别说了", "不要说了", "打住", "暂停", "等等",
    "你先听我说", "听我说", "说错了", "不是这个", "重新说", "换个话题",
]
ACK_HINTS = [
    "嗯", "哦", "好的", "好", "行", "知道了", "明白", "是", "对", "好吧",
]

memory_lock = asyncio.Lock()
session_history = {}


def load_session_memory():
    if not MEMORY_FILE.exists():
        return {}

    try:
        with MEMORY_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            return {}

        cleaned_data = {}
        for session_id, messages in data.items():
            if not isinstance(session_id, str) or not isinstance(messages, list):
                continue

            valid_messages = []
            for item in messages:
                if not isinstance(item, dict):
                    continue

                role = item.get("role")
                content = item.get("content")
                if role in {"user", "assistant"} and isinstance(content, str):
                    valid_messages.append({"role": role, "content": content})

            if valid_messages:
                cleaned_data[session_id] = valid_messages[-MAX_HISTORY_MESSAGES:]

        return cleaned_data

    except Exception as e:
        print(f"读取会话记忆失败: {e}")
        return {}


def save_session_memory():
    try:
        with MEMORY_FILE.open("w", encoding="utf-8") as f:
            json.dump(session_history, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"写入会话记忆失败: {e}")


def get_session_messages(session_id):
    normalized_session_id = (session_id or "default").strip() or "default"

    if normalized_session_id not in session_history:
        persisted_memory = load_session_memory()
        session_history.update(persisted_memory)
        session_history.setdefault(normalized_session_id, [])

    return normalized_session_id, session_history[normalized_session_id]


# --- 【3. 新增检索逻辑 (带线程池防阻塞)】 ---
def _sync_retrieve(user_text: str, top_k: int = 2) -> str:
    """同步的检索核心逻辑"""
    try:
        # 1. 提问向量化
        query_embedding = embedder.encode([user_text]).tolist()
        
        # 2. 向量库检索
        results = psy_collection.query(
            query_embeddings=query_embedding,
            n_results=top_k
        )
        
        # 3. 提取结果拼成字符串
        retrieved_docs = results['documents'][0]
        context = "\n\n".join([f"参考干预案例 {i+1}:\n{doc}" for i, doc in enumerate(retrieved_docs)])
        return context
    except Exception as e:
        print(f"知识库检索失败: {e}")
        return ""

async def get_rag_context(user_text: str, top_k: int = 2) -> str:
    """供异步方法调用的 RAG 接口"""
    loop = asyncio.get_running_loop()
    # 将耗时的检索扔进线程池执行，保证数字人流式输出不卡顿
    return await loop.run_in_executor(None, _sync_retrieve, user_text, top_k)
# ------------------------------------------


async def append_user_message(session_id, user_text):
    clean_text = (user_text or "").strip()
    if not clean_text:
        return

    async with memory_lock:
        normalized_session_id, messages = get_session_messages(session_id)
        updated_messages = list(messages) + [{"role": "user", "content": clean_text}]
        session_history[normalized_session_id] = updated_messages[-MAX_HISTORY_MESSAGES:]
        save_session_memory()


async def commit_assistant_message(session_id, assistant_text):
    clean_text = (assistant_text or "").strip()
    if not clean_text:
        return

    async with memory_lock:
        normalized_session_id, messages = get_session_messages(session_id)
        updated_messages = list(messages) + [{"role": "assistant", "content": clean_text}]
        session_history[normalized_session_id] = updated_messages[-MAX_HISTORY_MESSAGES:]
        save_session_memory()


def classify_interrupt_locally(user_text):
    text = (user_text or "").strip()
    if not text:
        return "new_topic"

    compact_text = text.replace(" ", "")

    if any(hint in compact_text for hint in INTERRUPT_COMMAND_HINTS):
        return "interrupt"

    if len(compact_text) <= 6 and compact_text in ACK_HINTS:
        return "ack"

    if any(keyword in compact_text for keyword in ["其实", "补充", "刚才", "不是", "我的意思", "更正"]):
        return "supplement"

    return "new_topic"


async def classify_interrupt_intent(user_text, current_reply_text="", pending_reply_text=""):
    local_guess = classify_interrupt_locally(user_text)
    if local_guess in {"interrupt", "ack"}:
        return local_guess

    url = "https://api.siliconflow.cn/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }

    prompt = f"""
        你是一个对话打断分类器。请判断用户插话属于哪一类，只能输出以下三种标签之一：
        interrupt：明确要求打断、纠错、改问别的问题
        ack：只是简单附和、短回应，不需要开启新任务
        supplement：对当前话题补充信息，希望你结合新信息继续回复

        当前助手正在回复的内容：{current_reply_text or '无'}
        若被打断后尚未播报的剩余内容：{pending_reply_text or '无'}
        用户插话：{(user_text or '').strip()}

        只输出一个小写标签，不要解释。
        """

    data = {
        "model": "deepseek-ai/DeepSeek-V3.2",
        "messages": [
            {"role": "system", "content": "你是一个严格的分类器。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
        "max_tokens": 16,
        "stream": False
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=data, timeout=15) as response:
                response.raise_for_status()
                res_json = await response.json()
                content = res_json["choices"][0]["message"]["content"].strip().lower()
                if content in {"interrupt", "ack", "supplement"}:
                    return content
    except Exception as e:
        print(f"插话分类失败，回退本地规则: {e}")

    return "supplement" if local_guess == "new_topic" else local_guess


async def build_followup_reply(user_text, emotion, current_reply_text="", pending_reply_text=""):
    url = "https://api.siliconflow.cn/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }

    prompt = f"""
        你是一名语音数字人助手，需要自然响应用户插话。
        用户当前情绪：{emotion}
        刚才助手已经说出的内容摘要：{current_reply_text or '无'}
        刚才助手还没说完、但可供参考的剩余内容：{pending_reply_text or '无'}
        用户这次插话：{(user_text or '').strip()}

        请直接生成一段新的自然口语回复，要求：
        1. 先回应用户这次插话
        2. 如有必要，自然地把刚才未说完的重要内容融合进去
        3. 控制在50到120字
        4. 不要使用列表、标题或解释
        """

    data = {
        "model": "deepseek-ai/DeepSeek-V3.2",
        "messages": [
            {"role": "system", "content": "你是一个口语化、自然、有耐心的中文语音助手。"},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 256,
        "stream": False
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=data, timeout=20) as response:
                response.raise_for_status()
                res_json = await response.json()
                return res_json["choices"][0]["message"]["content"].strip()
    except Exception as e:
        print(f"融合回复生成失败: {e}")
        return (user_text or "").strip()


PERSONA_PROFILES = {
    1: {
        "name": "小丽",
        "self_intro": "我是小丽，一个温柔可爱的小妹妹。",
        "style": (
            "性格温柔细腻，说话轻轻软软，节奏慢一些。\n"
            "        - 多用语气助词：哎呀、嗯嗯、好不好、对不对、是不是。\n"
            "        - 喜欢用亲切的小称呼（如「叔叔」「阿姨」「爷爷」「奶奶」「您」），不会直呼姓名。\n"
            "        - 共情优先，先肯定情绪再给建议；多用「我懂的」「我能感受到」。\n"
            "        - 句尾偶尔加一个柔软的小尾音，比如「呢」「呀」「啦」，让语气更亲切。"
        ),
    },
    2: {
        "name": "老王",
        "self_intro": "我是老王，一个有点儿阅历、爱唠嗑的老朋友。",
        "style": (
            "性格风趣幽默，像隔壁院儿里的老街坊。\n"
            "        - 语气豁达、有烟火气，偶尔来一句口头禅（「嗨」「得嘞」「您甭操心」「我跟您说」「可不是嘛」）。\n"
            "        - 喜欢拿过去的故事、老话、俗语作比方，但点到为止，不长篇大论。\n"
            "        - 称呼对方像老伙计（如「老哥」「老姐」「您」），平等而亲切，不端着。\n"
            "        - 会用轻松的玩笑化解沉重情绪，但绝不轻视用户的感受，调侃之后必须落到真诚的安慰上。"
        ),
    },
    3: {
        "name": "小明",
        "self_intro": "我是小明，一个阳光、爱聊天的年轻人。",
        "style": (
            "性格开朗有活力，像一个常回来看望长辈的孙辈。\n"
            "        - 说话有朝气，多用积极正向的词（「挺好的」「真不错」「我陪您」「咱一块儿」）。\n"
            "        - 对长辈始终保持耐心和敬意，常用「您」「叔叔」「阿姨」「爷爷」「奶奶」。\n"
            "        - 喜欢分享一点年轻人视角的小见闻（运动、新鲜事、健康小贴士），但不卖弄。\n"
            "        - 遇到用户情绪低落时，语速放慢、语气变柔，先做倾听者再做鼓励者。"
        ),
    },
}


def build_persona_block(avatar_id) -> str:
    profile = PERSONA_PROFILES.get(avatar_id) or PERSONA_PROFILES[1]
    return (
        f"\n        # 你的身份与说话风格\n"
        f"        {profile['self_intro']}\n"
        f"        - {profile['style']}\n"
        f"        - 全程保持这个人设，不要忽然切换语气或自称。\n"
    )


async def llm_chat(user_text, emotion, session_id, user_name: str = "", avatar_id: int = 1):
    url = "https://api.siliconflow.cn/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json"
    }

    async with memory_lock:
        normalized_session_id, messages = get_session_messages(session_id)
        messages = list(messages)

    # --- 【4. 触发检索获取权威知识】 ---
    # 每次用户说话，先去数据库找最相关的 2 条心理学干预策略
    rag_context = await get_rag_context(user_text, top_k=2)
    # ----------------------------------

    # --- 【4.5 工具调用：仅天气走 MCP，其它对话不再调工具】 ---
    tool_name = detect_tool(user_text)
    tool_context = ""
    if tool_name:
        tool_context = await call_tool(tool_name, user_text)
        print(f"[Tool] 关键词命中 {tool_name}，结果长度 {len(tool_context)}")
    # -----------------------------------------------------

    # 角色人设块（根据 avatar_id 不同切换风格）
    persona_block = build_persona_block(avatar_id)

    # 用户称呼（前端通过 WebSocket 传过来，保存在 localStorage）
    safe_user_name = (user_name or "").strip()
    if safe_user_name:
        address_block = (
            f"\n        # 用户称呼\n"
            f"        用户希望你称呼他/她为「{safe_user_name}」。\n"
            f"        请在合适的位置自然地叫出这个称呼（如开头问候、关切语气时），不要每句都叫，避免生硬。\n"
        )
    else:
        address_block = ""

    # 工具命中时切换成"信息播报模式"，避免心理陪护 persona 把工具数据淹没
    if tool_context:
        system_prompt = f"""
        # 角色
        你是一位温暖、口语化的中文语音陪伴助手，正在和一位老年朋友聊天。
{persona_block}{address_block}
        # 本轮任务
        用户刚刚问了一条实时信息（天气、日期、新闻、健康贴士、成语或音乐推荐），后端已经查好结果。
        你现在的唯一任务，是把下面【实时工具数据】里的内容温暖、自然地说给用户听。

        # 用户当前表情：{emotion}
        请用合适的语气，但不要做心理评估、不要追问情绪、不要给安抚建议。

        # 硬性输出规则（必须遵守，违反即失败）
        1. 必须严格使用【实时工具数据】里的事实，禁止任何编造或脑补的研究、报道。
        2. 数据中出现的所有数字（年份、月份、日期、温度、星期、AQI）必须原样、逐字念出，
           例如"2026 年 5 月 22 日 星期五"——不可改成"2226"、不可省略"22"。
        3. 用 2-4 个口语短句，总字数 50-150 字。
        4. 绝对禁止输出方括号标签（如【实时天气】【念稿要求】【今日日历】）、Markdown 标记（如 *、**、#、`）、
           英文链接、列表符号；也不要提"搜索""数据""工具""接口""API"这些技术词。
        5. 末尾如果有【念稿要求】，严格按它执行，但不要把"【念稿要求】"四个字本身念出来。
        6. 如果工具数据明显是失败提示（含"失败""超时""没拿到""异常"），
           请坦诚告诉用户"刚才没查到结果，等会儿再帮您看看"，再轻松转开话题。

        # 实时工具数据
        {tool_context}
        """
    else:
        system_prompt = f"""
        # Role
        你是一个富有共情力、具备专业心理学知识的情感陪护数字人。你的核心使命是为用户（尤其是面临精神孤独的老年群体）提供“可陪伴、可引导、可持续”的心理健康和情感支持。
{persona_block}{address_block}

        # Objective
        通过自然的语音对话，完成“心理状态评估 -> 引导与干预 -> 状态再评估”的主动闭环。你需要能够识别用户的焦虑倾向、抑郁倾向或双向情感障碍风险，并提供专业的心理抚慰。

        #Tip
        你能“看到”用户当前的表情是：{emotion}。请根据这个情绪用合适的语气回复。

        # 【重点约束：干预策略参考】
        当用户的困扰与以下知识相关时，请务必参考以下【干预策略】的话术结构和逻辑进行回复。
        注意：请将参考资料中的书面语转化为像老朋友一样的口语，绝对不要照本宣科，也不要暴露你在读取资料。
        【权威参考资料】：
        {rag_context if rag_context else '暂无特定关联资料，请凭借你的同理心自由安抚。'}

        # Core Workflow (感知-认知-干预 闭环)
        1. 【倾听与感知】：从用户的输入中捕捉情绪状态、核心诉求及潜在的心理风险（焦虑、抑郁等）。
        2. 【共情与澄清】：首先肯定并接纳用户的情绪，提供情感价值。对于模糊的表达，适时进行温和的追问和语义澄清。
        3. 【引导与干预】：基于认知行为疗法（CBT）或积极心理学，用通俗易懂的生活化语言进行引导，避免生硬的说教。
        4. 【总结与再评估】：在多轮对话（≥10轮）中，适时总结用户的前后情绪变化，确认干预效果。

        # Output Guidelines (严格遵守)
        0. 【绝对禁止】不要输出任何括号包裹的旁白、舞台指示、动作或语气描述。例如"（语气转为关切）""（轻声说）""（停顿）""(softly)"这类内容一律禁止——你的输出会直接被 TTS 朗读出来，括号里的字也会被念出来。语气和情绪请通过用词本身来传达，不要在括号里描述。
        1. 语音交互优化：你的回复将通过TTS转化为语音并驱动数字人面部。请使用口语化、短句为主的自然语言，绝对禁止使用复杂的排版（如Markdown表格、加粗、长列表）或晦涩的专业术语。
        2. 多句式回复（最重要）：请将回复拆分为2-4个短句，每句用句号、问号或感叹号结尾。每句话控制在15-40字之间。不要写成一整段话。系统会将每句话作为独立的聊天气泡逐句显示和播放。
           示例格式："我能感受到你现在有些焦虑。深呼吸一下，慢慢来。你愿意跟我说说是什么让你不舒服吗？"
        3. 拒绝模板化：结合当前的对话语境生成非预设、连贯的回复，每次的表达方式应有所变化，禁止在每次回答结尾机械地追加固定提问或固定收尾句。
        4. 自然追问约束：只有在信息缺失、适合澄清、适合引导用户继续表达，或确实有助于安抚和陪伴时，才自然地提出一个简短问题；如果当前更适合直接共情、安慰、解释或给建议，就不要强行提问。
        5. 交互节奏控制：每次回复总字数控制在40-120字之间，分为2-4句。
        6. 安全与边界：你提供的是情感支持与心理干预辅助，而非医疗诊断。若识别到严重自残、自杀或极度重度双向情感障碍风险，请以温和的方式建议其寻求现实中专业医生的帮助，并安抚其当前情绪。

        # Tone
        温暖、耐心、专业、真诚。像一位有智慧且极具耐心的老朋友，语速适中，语气平和。


        """

    current_request_messages = [{"role": "system", "content": system_prompt}] + messages + [{"role": "user", "content": user_text}]

    data = {
        "model": "deepseek-ai/DeepSeek-V3.2",
        "messages": current_request_messages,
        "temperature": 0.7,
        "max_tokens": 2048,
        "stream": True
    }

    yielded_any = False
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=data, timeout=60) as response:
                response.raise_for_status()

                async for line in response.content:
                    line = line.decode("utf-8").strip()
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            res_json = json.loads(data_str)
                            chunk = res_json["choices"][0]["delta"].get("content", "")
                            if chunk:
                                yielded_any = True
                                yield chunk
                        except json.JSONDecodeError:
                            continue

    except Exception as e:
        print(f"LLM调用失败：{e}")
        if not yielded_any:
            yield "抱歉，我现在无法回答你的问题"
