import asyncio
import re
import json
import base64
import time
import io
import wave
import struct
import traceback
import mimetypes
import numpy as np
import contextlib
from itertools import count
from dotenv import load_dotenv

# Ensure .mjs files are served with the correct JavaScript MIME type
mimetypes.add_type('text/javascript', '.mjs')

load_dotenv()  # 自动读取同目录的 .env 文件


from typing import Dict, Any, Optional
from dataclasses import dataclass, field
from starlette.websockets import WebSocketState
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from llm import (
    llm_chat,
    append_user_message,
    commit_assistant_message,
    classify_interrupt_intent,
    build_followup_reply,
)
from user_profile import record_turn, delete_profile
from asr import asr_engine  # 此时 asr.py 内部已改为加载 whisper-final-model
from emotion import get_face_emotion
from tts import tts_engine, TTSGenerationError

from faceformer_adapter import audio_bytes_to_user_emotion
from tools import get_weather, get_calendar, get_news
import reminders as reminders_mod

app = FastAPI()

# 允许跨域
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PUNCTUATION_PATTERN = re.compile(r'([。，！？,.!?])')
SENTENCE_END_PATTERN = re.compile(r'([。！？!?])')  # 句末标点，强制分割
MIN_CHUNK_LENGTH = 14
MAX_CHUNK_LENGTH = 38
MIN_SENTENCE_LENGTH = 4  # 句末标点分割的最小长度（防止空句）
TTS_PREFETCH_CHUNKS = 2
# 纯"非内容字符"判定：含中英文标点、引号、括号、空白
PUNCTUATION_ONLY_PATTERN = re.compile(r'^[\s。，！？,.!?、；;：:~～…“”"\'\'「」『』《》（）\(\)\[\]【】—\-—\.]+$')

# 清洗 LLM 偶尔泄露的工具标签和 markdown 标记
_META_TAG_PATTERN = re.compile(r'【[^】]{0,30}】')          # 去掉 【实时天气】【念稿要求】等元标签
_STAGE_DIRECTION_PATTERN = re.compile(r'（[^）]{1,40}）')   # 去掉 （语气转为关切）（轻声说）等中文括号旁白
_STAGE_DIRECTION_ASCII_PATTERN = re.compile(r'\([^)\d]{2,40}\)')  # 去掉 (softly) (pause) 等英文小括号旁白；保留数字括号如 (30%)
_MARKDOWN_BOLD_PATTERN = re.compile(r'\*{1,3}')             # ** 加粗
_MARKDOWN_HEADING_PATTERN = re.compile(r'^#{1,6}\s*', re.MULTILINE)
_MARKDOWN_CODE_PATTERN = re.compile(r'`{1,3}')
_BACKSLASH_NEWLINE_PATTERN = re.compile(r'\\n')
_MULTI_SPACE_PATTERN = re.compile(r'[ \t]{2,}')


def sanitize_chunk(text: str) -> str:
    """去掉模型偶尔泄露的工具元标签、markdown 标记，避免出现在聊天气泡里。"""
    if not text:
        return ''
    text = _META_TAG_PATTERN.sub('', text)
    text = _STAGE_DIRECTION_PATTERN.sub('', text)
    text = _STAGE_DIRECTION_ASCII_PATTERN.sub('', text)
    text = _MARKDOWN_HEADING_PATTERN.sub('', text)
    text = _MARKDOWN_BOLD_PATTERN.sub('', text)
    text = _MARKDOWN_CODE_PATTERN.sub('', text)
    text = _BACKSLASH_NEWLINE_PATTERN.sub(' ', text)
    text = _MULTI_SPACE_PATTERN.sub(' ', text)
    return text.strip()

# 1. 静态资源托管（限制只能访问 public 文件夹里的前端文件）
app.mount("/static", StaticFiles(directory="./public"), name="static")

# 每秒最多做一次情绪识别，防止摄像头帧把线程池打爆
EMOTION_INTERVAL = 1.0
#全局线程锁
send_lock = asyncio.Lock()
response_id_counter = count(1)


@dataclass
class ResponseState:
    response_id: int
    session_id: str
    user_text: str
    full_text: str = ""
    spoken_chunks: list = field(default_factory=list)
    pending_chunks: list = field(default_factory=list)
    interrupted: bool = False
    completed: bool = False
    chunk_seq: int = 0
    llm_done: bool = False
    first_chunk_at: Optional[float] = None
    started_at: float = field(default_factory=time.time)

    def spoken_text(self):
        return "".join(self.spoken_chunks).strip()

    def pending_text(self):
        return "".join(self.pending_chunks).strip()

    def mark_chunk_enqueued(self, clean_text: str) -> int:
        if clean_text not in self.pending_chunks and clean_text not in self.spoken_chunks:
            self.pending_chunks.append(clean_text)
        seq = self.chunk_seq
        self.chunk_seq += 1
        if self.first_chunk_at is None:
            self.first_chunk_at = time.time()
        return seq

    def mark_chunk_done(self, clean_text: str):
        if clean_text in self.pending_chunks:
            self.pending_chunks.remove(clean_text)
        if clean_text not in self.spoken_chunks:
            self.spoken_chunks.append(clean_text)


def resolve_role(avatar_id):
    if avatar_id == 1:
        return "girl"
    if avatar_id == 2:
        return "elderly"
    if avatar_id == 3:
        return "boy"
    return "girl"


# =========================================================
# 通用安全发送
# =========================================================
async def safe_send(ws: WebSocket, data: Dict[str, Any]) -> bool:
    try:
        if ws.client_state == WebSocketState.CONNECTED:
            async with send_lock:
                await ws.send_json(data)
            return True

    except asyncio.CancelledError:
        print("任务被打断")
        raise

    except Exception as e:
        print("发送失败:", e)
    return False


# =========================================================
# PCM数组转WAV
# =========================================================
def pcm_list_to_wav_bytes(audio_data):
    raw_pcm = struct.pack(f"<{len(audio_data)}h", *audio_data)

    wav_io = io.BytesIO()
    with wave.open(wav_io, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(16000)
        wav_file.writeframes(raw_pcm)

    return wav_io.getvalue()


async def send_stop_output(websocket: WebSocket, response_id: int, reason="barge_in"):
    await safe_send(websocket, {
        "type": "stop_output",
        "responseId": response_id,
        "reason": reason,
        "stopAudio": True,
        "stopBlendshape": True,
        "enterListening": True,
    })
    await safe_send(websocket, {
        "type": "listen_state",
        "state": "listening",
        "responseId": response_id,
        "microActions": ["eye_contact", "nod"],
    })


# =========================================================
# 文本生成语音+口型并发送
# =========================================================
async def send_chunk_packet(websocket, response_state, seq, clean_text, audio_bytes, visemes, is_current_response, emotion="neutral"):
    if not is_current_response(response_state.response_id):
        response_state.interrupted = True
        return False

    payload = {
        "type": "assistant_chunk",
        "responseId": response_state.response_id,
        "seq": seq,
        "text": clean_text,
        "fullText": response_state.full_text,
        "isFinalChunk": False,
    }

    if audio_bytes:
        payload["audio"] = base64.b64encode(audio_bytes).decode("utf-8")

    if visemes:  # 非空列表才发送，减少带宽
        payload["visemes"] = visemes

    if emotion and emotion != "neutral":
        payload["emotion"] = emotion

    sent = await safe_send(websocket, payload)
    if sent:
        response_state.mark_chunk_done(clean_text)
    return sent


async def synthesize_chunk_pipeline(clean_text, role):
    # 使用 generate_audio_bytes_with_visemes() 同时获取音频和 Viseme 口型时间轴。
    # visemes: list[{"time_ms": int, "viseme_id": int}]，由 edge-tts WordBoundary 事件产出。
    audio_bytes, visemes = await tts_engine.generate_audio_bytes_with_visemes(
        clean_text, role=role
    )
    return audio_bytes, visemes


async def speak_and_send(websocket, text_chunk, avatar_id, response_state, is_current_response):
    clean_text = (text_chunk or "").strip()
    if not clean_text:
        return False

    if not is_current_response(response_state.response_id):
        response_state.interrupted = True
        return False

    role = resolve_role(avatar_id)
    seq = response_state.mark_chunk_enqueued(clean_text)

    try:
        audio_bytes, visemes = await synthesize_chunk_pipeline(clean_text, role)
        return await send_chunk_packet(
            websocket,
            response_state,
            seq,
            clean_text,
            audio_bytes,
            visemes,
            is_current_response,
        )

    except asyncio.CancelledError:
        response_state.interrupted = True
        raise

    except TTSGenerationError as e:
        print(f"[TTS] 合成失败，回退文本: seq={seq}, 错误={e}")
        if is_current_response(response_state.response_id):
            sent = await safe_send(websocket, {
                "type": "assistant_chunk",
                "responseId": response_state.response_id,
                "seq": seq,
                "text": clean_text,
                "fullText": response_state.full_text,
                "isFinalChunk": False,
                "fallback": "text_only",
            })
            if sent:
                response_state.mark_chunk_done(clean_text)
            return sent
        return False


# ── 文本情感检测（关键词匹配，零延迟）───────────────────────────────────────
_EMOTION_KW = {
    "happy":    ["开心", "高兴", "太好了", "太棒了", "真棒", "厉害", "加油", "相信你", "没问题",
                 "放心", "很好", "不错", "好极了", "完美", "恭喜", "祝贺", "愉快", "轻松", "快乐"],
    "sad":      ["难过", "伤心", "担心", "心疼", "辛苦", "委屈", "不容易", "艰难", "痛苦",
                 "难受", "悲伤", "眼泪", "哭泣", "遗憾", "惋惜"],
    "surprise": ["真的吗", "没想到", "居然", "竟然", "哇", "原来如此", "太意外", "太惊讶"],
    "caring":   ["理解你", "明白你", "能感受到", "陪着你", "慢慢来", "没关系", "安心",
                 "我在这", "不用担心", "都会好的", "一起", "支持你", "温暖"],
}

def detect_text_emotion(text: str) -> str:
    for emotion, keywords in _EMOTION_KW.items():
        if any(kw in text for kw in keywords):
            return emotion
    return "neutral"


def is_tts_speakable_text(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False
    return PUNCTUATION_ONLY_PATTERN.fullmatch(stripped) is None


def has_unclosed_bracket(text: str) -> bool:
    """检查文本里是否存在尚未闭合的括号（中文/英文小括号）。
    用于流式切分时避免把 LLM 偶尔输出的 `（语气舒缓，...）` 旁白切两半，
    导致 sanitize_chunk 的整段括号正则匹配不到。"""
    depth_cn = text.count('（') - text.count('）')
    depth_en = text.count('(') - text.count(')')
    return depth_cn > 0 or depth_en > 0


def should_emit_chunk(buffer_text: str) -> bool:
    """判断 buffer 中是否有可以切出的 chunk"""
    stripped = (buffer_text or "").strip()
    if not stripped:
        return False
    if PUNCTUATION_ONLY_PATTERN.fullmatch(stripped):
        return False
    # 括号未闭合时禁止切分，等收齐 `）` 再说（防止旁白被切碎漏过清洗）
    # 兜底：buffer 太长就算括号没闭合也强切，避免模型忘了闭合卡死整个流
    if has_unclosed_bracket(stripped) and len(stripped) < MAX_CHUNK_LENGTH * 2:
        return False
    # 优先：句末标点（。！？!?）只要长度 >= MIN_SENTENCE_LENGTH 就可以分割
    if SENTENCE_END_PATTERN.search(stripped) and len(stripped) >= MIN_SENTENCE_LENGTH:
        return True
    # 次优先：句中标点（，,）需要长度 >= MIN_CHUNK_LENGTH
    if PUNCTUATION_PATTERN.search(stripped) and len(stripped) >= MIN_CHUNK_LENGTH:
        return True
    # 兜底：超长文本强制分割
    return len(stripped) >= MAX_CHUNK_LENGTH


def _is_inside_bracket(text: str, pos: int) -> bool:
    """判断 text[pos] 是否落在尚未闭合的括号内（中文/英文小括号）。
    用于切分时跳过括号内的标点，防止 `（语气舒缓，...）` 被从内部逗号处切两半。"""
    prefix = text[:pos]
    if prefix.count('（') > prefix.count('）'):
        return True
    if prefix.count('(') > prefix.count(')'):
        return True
    return False


def split_next_chunk(buffer_text: str):
    """从 buffer 中切出一个 chunk，返回 (chunk, rest)"""
    working = buffer_text or ""
    stripped_working = working.strip()
    if PUNCTUATION_ONLY_PATTERN.fullmatch(stripped_working):
        return "", ""

    # 策略1：优先在句末标点（。！？!?）处分割，找最靠前的满足最小长度的句末标点
    for m in SENTENCE_END_PATTERN.finditer(working):
        if m.end() >= MIN_SENTENCE_LENGTH and not _is_inside_bracket(working, m.start()):
            chunk = working[:m.end()].strip()
            rest = working[m.end():].lstrip()
            return chunk, rest

    # 策略2：在句中标点（，,等）处分割，遍历所有匹配找到位置 >= MIN_CHUNK_LENGTH 的
    for m in PUNCTUATION_PATTERN.finditer(working):
        if m.end() >= MIN_CHUNK_LENGTH and not _is_inside_bracket(working, m.start()):
            chunk = working[:m.end()].strip()
            rest = working[m.end():].lstrip()
            return chunk, rest

    # 策略3：超长文本强制分割
    if len(stripped_working) >= MAX_CHUNK_LENGTH:
        split_idx = MAX_CHUNK_LENGTH
        window = working[:MAX_CHUNK_LENGTH + 1]
        for delimiter in ["，", "、", "；", ",", " "]:
            idx = window.rfind(delimiter)
            if idx >= MIN_SENTENCE_LENGTH:
                split_idx = idx + 1
                break
        return working[:split_idx].strip(), working[split_idx:].lstrip()

    # 无法分割
    return "", working


# =========================================================
# 单轮对话处理
# =========================================================
async def process_user_message(
    websocket: WebSocket,
    user_text: str,
    emotion: str,
    session_id: str,
    avatar_id,
    response_state: ResponseState,
    is_current_response,
    preset_reply: str = None,
    user_name: str = "",
    user_id: str = "",
    city: str = "",
):
    chunk_queue = asyncio.Queue()
    sentinel = object()
    has_sent = False
    role = resolve_role(avatar_id)
    worker_task = None

    async def tts_worker():
        nonlocal has_sent
        pending_tasks = set()

        async def schedule_chunk(clean_text: str):
            if not is_tts_speakable_text(clean_text):
                return
            task = asyncio.create_task(synthesize_chunk_pipeline(clean_text, role))
            task.clean_text = clean_text
            task.seq = response_state.mark_chunk_enqueued(clean_text)
            task.emotion = detect_text_emotion(clean_text)
            pending_tasks.add(task)

        async def flush_ready(force=False):
            nonlocal has_sent
            while pending_tasks:
                next_task = min(pending_tasks, key=lambda item: item.seq)
                if not force and not next_task.done():
                    break
                pending_tasks.remove(next_task)
                clean_text = next_task.clean_text
                seq = next_task.seq
                try:
                    audio_bytes, visemes = await next_task
                    sent = await send_chunk_packet(
                        websocket,
                        response_state,
                        seq,
                        clean_text,
                        audio_bytes,
                        visemes,
                        is_current_response,
                        emotion=getattr(next_task, "emotion", "neutral"),
                    )
                    has_sent = has_sent or sent or bool(clean_text)
                except asyncio.CancelledError:
                    raise
                except TTSGenerationError as e:
                    print(f"TTS生成失败，降级为文本发送: seq={seq}, error={e}")
                    if is_current_response(response_state.response_id):
                        sent = await safe_send(websocket, {
                            "type": "assistant_chunk",
                            "responseId": response_state.response_id,
                            "seq": seq,
                            "text": clean_text,
                            "fullText": response_state.full_text,
                            "isFinalChunk": False,
                            "fallback": "text_only",
                        })
                        if sent:
                            response_state.mark_chunk_done(clean_text)
                        has_sent = has_sent or sent or bool(clean_text)
                except Exception as e:
                    print(f"分片发送失败: seq={seq}, error={e}")

        try:
            while True:
                item = await chunk_queue.get()
                if item is sentinel:
                    break

                await schedule_chunk(item)
                if len(pending_tasks) >= TTS_PREFETCH_CHUNKS:
                    await flush_ready(force=False)

            while pending_tasks:
                if not is_current_response(response_state.response_id):
                    response_state.interrupted = True
                    for task in pending_tasks:
                        task.cancel()
                    break
                await flush_ready(force=True)

        finally:
            for task in list(pending_tasks):
                if not task.done():
                    task.cancel()
            for task in list(pending_tasks):
                with contextlib.suppress(Exception):
                    await task

    try:
        sentence_buffer = ""
        full_reply = ""

        await append_user_message(session_id, user_text)
        worker_task = asyncio.create_task(tts_worker())

        async def handle_text_piece(text_piece):
            nonlocal sentence_buffer, full_reply
            full_reply += text_piece
            response_state.full_text = full_reply
            sentence_buffer += text_piece

            while should_emit_chunk(sentence_buffer):
                text_chunk, sentence_buffer_rest = split_next_chunk(sentence_buffer)
                if not text_chunk:
                    break
                sentence_buffer = sentence_buffer_rest
                if not is_current_response(response_state.response_id):
                    response_state.interrupted = True
                    return
                # 清洗：去掉工具元标签 / markdown / 多余空格
                text_chunk = sanitize_chunk(text_chunk)
                if not text_chunk or PUNCTUATION_ONLY_PATTERN.fullmatch(text_chunk):
                    continue  # 清洗后空了或纯标点，跳过不发
                await chunk_queue.put(text_chunk)
                await safe_send(websocket, {
                    "type": "assistant_text_delta",
                    "responseId": response_state.response_id,
                    "delta": text_chunk,
                    "fullText": response_state.full_text,
                })

        await safe_send(websocket, {
            "type": "turn_start",
            "responseId": response_state.response_id,
            "userText": user_text,
        })

        if preset_reply is not None:
            for char in preset_reply:
                await handle_text_piece(char)
                if not is_current_response(response_state.response_id):
                    response_state.interrupted = True
                    break
        else:
            async for char in llm_chat(user_text, emotion, session_id, user_name=user_name, avatar_id=avatar_id, user_id=user_id, city=city):
                if not is_current_response(response_state.response_id):
                    response_state.interrupted = True
                    break
                await handle_text_piece(char)

        if sentence_buffer.strip() and is_current_response(response_state.response_id):
            tail_text = sanitize_chunk(sentence_buffer.strip())
            if tail_text and is_tts_speakable_text(tail_text):
                await chunk_queue.put(tail_text)
                await safe_send(websocket, {
                    "type": "assistant_text_delta",
                    "responseId": response_state.response_id,
                    "delta": tail_text,
                    "fullText": response_state.full_text,
                })

        response_state.llm_done = True
        await chunk_queue.put(sentinel)
        if worker_task:
            await worker_task

        if not has_sent and is_current_response(response_state.response_id):
            fallback_text = "抱歉，我刚才走神了，您可以再说一遍吗？"
            full_reply = fallback_text
            response_state.full_text = fallback_text
            sent = await speak_and_send(
                websocket,
                fallback_text,
                avatar_id,
                response_state,
                is_current_response,
            )
            has_sent = has_sent or sent or bool(fallback_text)

        if is_current_response(response_state.response_id) and not response_state.interrupted:
            response_state.completed = True
            await commit_assistant_message(session_id, response_state.full_text)
            asyncio.create_task(record_turn(user_id or session_id, user_text, response_state.full_text))

    except asyncio.CancelledError:
        response_state.interrupted = True
        raise

    except Exception as e:
        print(f"[chat] 处理对话异常: {e}")
        if is_current_response(response_state.response_id):
            await safe_send(websocket, {"type": "error", "message": "处理消息时出错了"})

    finally:
        if worker_task and not worker_task.done():
            worker_task.cancel()
            with contextlib.suppress(Exception):
                await worker_task

        if is_current_response(response_state.response_id) and not response_state.interrupted:
            await safe_send(websocket, {
                "type": "turn_end",
                "responseId": response_state.response_id,
                "fullText": response_state.full_text,
            })
        elif response_state.interrupted:
            await safe_send(websocket, {
                "type": "turn_interrupted",
                "responseId": response_state.response_id,
                "spokenText": response_state.spoken_text(),
                "pendingText": response_state.pending_text(),
            })


# 2. 根目录返回 index.html（也要对应修改路径）
@app.get("/")
async def get_index():
    return FileResponse("./public/index.html")


# =========================================================
# 实时数据接口（给右侧信息卡用）
# =========================================================
@app.get("/api/weather")
async def api_weather(city: Optional[str] = None):
    data = await get_weather(city)
    return JSONResponse(data)


@app.get("/api/news")
async def api_news(topic: Optional[str] = None):
    data = await get_news(topic)
    return JSONResponse(data)


@app.get("/api/calendar")
async def api_calendar():
    return JSONResponse({"ok": True, **get_calendar()})

# ==========================================
# 【漏洞1修补】：官方 ASR 评测打分接口
# ==========================================
@app.post("/asr")
async def evaluate_asr(request: Request):
    try:
        audio_bytes = await request.body()

        if len(audio_bytes) > 10 * 1024 * 1024:
            return JSONResponse(
                {"result": "文件过大"},
                status_code=413
            )

        if not audio_bytes:
            return JSONResponse({"result": ""})

        loop = asyncio.get_running_loop()

        text = await loop.run_in_executor(
            None,
            asr_engine.speech_to_text,
            audio_bytes
        )

        return JSONResponse({"result": text})

    except Exception as e:
        return JSONResponse({"result": f"识别错误:{str(e)}"})

# =========================================================
# WebSocket 主入口
# =========================================================
@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    await websocket.accept()
    print("客户端已连接")

    tasks = set()

    # 当前正在执行的聊天任务（只允许一个）
    current_chat_task = None
    current_response_id = 0
    active_response_state = None

    current_emotion = "neutral"
    current_session_id = "default"
    current_avatar_id = 1
    current_user_name = ""
    current_user_id = ""
    current_city = ""          # 由前端定位后通过 location / init 消息同步
    last_emotion_time = 0
    emotion_busy = False

    def is_current_response(response_id: int) -> bool:
        return response_id == current_response_id

    async def start_chat_task(user_text: str, preset_reply: str = None):
        nonlocal current_chat_task, current_response_id, active_response_state

        previous_state = active_response_state
        had_active_task = current_chat_task and not current_chat_task.done()

        if had_active_task or (previous_state and not previous_state.completed):
            if previous_state:
                previous_state.interrupted = True
            current_response_id = next(response_id_counter)
            await send_stop_output(websocket, current_response_id, reason="barge_in")
            if current_chat_task and not current_chat_task.done():
                current_chat_task.cancel()
        else:
            current_response_id = next(response_id_counter)

        current_state = ResponseState(
            response_id=current_response_id,
            session_id=current_session_id,
            user_text=user_text,
        )
        active_response_state = current_state

        current_chat_task = asyncio.create_task(
            process_user_message(
                websocket,
                user_text,
                current_emotion,
                current_session_id,
                current_avatar_id,
                current_state,
                is_current_response,
                preset_reply=preset_reply,
                user_name=current_user_name,
                user_id=current_user_id,
                city=current_city,
            )
        )

        tasks.add(current_chat_task)
        current_chat_task.add_done_callback(tasks.discard)
        return current_state

    async def handle_user_text(user_text: str):
        previous_state = active_response_state
        had_active_task = current_chat_task and not current_chat_task.done()
        preset_reply = None

        # ── 提醒意图优先于一切（打断分类 / LLM）──
        parsed = reminders_mod.try_parse_reminder(user_text)
        print(f"[reminder] user_text={user_text!r}  parsed={parsed}")
        if parsed:
            when_dt, content = parsed
            reminder = reminders_mod.add(when_dt, content)
            print(f"[reminder] added id={reminder.id} when={reminder.when_ts} content={content!r}")
            await safe_send(websocket, {
                "type": "reminder_added",
                "reminder": reminder.to_dict(),
            })
            ack = reminders_mod.build_ack_text(
                reminders_mod.format_when(reminder.when_ts),
                content,
                current_user_name,
            )
            await start_chat_task(user_text, preset_reply=ack)
            return

        if had_active_task and previous_state:
            predicted_response_id = current_response_id + 1
            await send_stop_output(websocket, predicted_response_id, reason="barge_in_detected")
            interrupt_type = await classify_interrupt_intent(
                user_text,
                previous_state.spoken_text() or previous_state.full_text,
                previous_state.pending_text(),
            )
            await safe_send(websocket, {
                "type": "interrupt_classified",
                "intent": interrupt_type,
                "responseId": predicted_response_id,
            })

            if interrupt_type == "ack":
                preset_reply = "嗯，我听到了。刚才的意思是，" + (previous_state.pending_text() or previous_state.full_text or "我们可以接着慢慢聊。")
            elif interrupt_type == "supplement":
                preset_reply = await build_followup_reply(
                    user_text,
                    current_emotion,
                    previous_state.spoken_text() or previous_state.full_text,
                    previous_state.pending_text(),
                )

        await start_chat_task(user_text, preset_reply=preset_reply)

    async def reminder_scanner():
        """每 5 秒扫一次提醒，到期就推前端 + 让数字人主动播报"""
        try:
            while True:
                await asyncio.sleep(5)
                due = reminders_mod.pop_due()
                for r in due:
                    await safe_send(websocket, {
                        "type": "reminder_fired",
                        "reminder": r.to_dict(),
                    })
                    fire_text = reminders_mod.build_fire_text(r.content, current_user_name)
                    await start_chat_task(f"[reminder:{r.id}]", preset_reply=fire_text)
        except asyncio.CancelledError:
            return

    scanner_task = asyncio.create_task(reminder_scanner())
    tasks.add(scanner_task)
    scanner_task.add_done_callback(tasks.discard)

    try:
        while True:
            data_str = await websocket.receive_text()

            try:
                payload = json.loads(data_str)
            except Exception as e:
                # 打印出具体的错误原因和前50个字符，方便你查错
                print(f"消息格式不对，解析失败: {data_str[:50]}... 具体原因: {e}")
                continue

            msg_type = payload.get("type")

            # -------------------------
            # 心跳机制
            # -------------------------
            if msg_type == "ping":
                await safe_send(websocket, {"type": "pong"})
                continue

            # -------------------------
            # 显式抢权开始：前端VAD检测到用户开始说话时可先发
            # -------------------------
            if msg_type == "barge_in_start":
                if current_chat_task and not current_chat_task.done():
                    if active_response_state:
                        active_response_state.interrupted = True
                    current_response_id = next(response_id_counter)
                    await send_stop_output(websocket, current_response_id, reason="barge_in_start")
                    current_chat_task.cancel()
                continue

            # -------------------------
            # 用户画像删除
            # -------------------------
            if msg_type == "delete_user":
                uid = (payload.get("userId") or "").strip()
                if uid:
                    await delete_profile(uid)
                continue

            # -------------------------
            # 会话管理
            # -------------------------
            if msg_type in ["init", "new_session", "switch_session"]:

                current_session_id = payload.get("sessionId", "default")

                incoming_uid = (payload.get("userId") or "").strip()
                if incoming_uid:
                    current_user_id = incoming_uid

                if msg_type == "init":
                    current_avatar_id = payload.get("avatarId", 1)
                    await safe_send(websocket, {
                        "type": "reminder_list",
                        "reminders": [r.to_dict() for r in reminders_mod.list_active()],
                    })

                incoming_name = (payload.get("userName") or "").strip()
                if incoming_name:
                    current_user_name = incoming_name

                # 前端若携带缓存城市（init 时 localStorage 已有）则同步
                incoming_city = (payload.get("city") or "").strip()
                if incoming_city:
                    current_city = incoming_city
                    print(f"[location] init 同步城市: {current_city}")

                continue

            # -------------------------
            # 前端定位城市上报（geolocation 异步完成后发送）
            # -------------------------
            if msg_type == "location":
                incoming_city = (payload.get("city") or "").strip()
                if incoming_city:
                    current_city = incoming_city
                    print(f"[location] 城市更新: {current_city}")
                continue

            # -------------------------
            # 摄像头帧情绪识别（限频）
            # -------------------------
            if msg_type == "frame":
                now = time.time()

                if now - last_emotion_time < EMOTION_INTERVAL:
                    continue

                if emotion_busy:
                    continue

                last_emotion_time = now
                emotion_busy = True

                try:
                    img_b64 = payload.get("data", "")

                    if img_b64.startswith("data:image"):
                        img_b64 = img_b64.split(",")[1]

                    img_bytes = base64.b64decode(img_b64)

                    loop = asyncio.get_running_loop()

                    current_emotion = await loop.run_in_executor(
                        None,
                        get_face_emotion,
                        img_bytes
                    )

                except Exception as e:
                    print("情绪识别失败:", e)

                finally:
                    emotion_busy = False

                continue

            # -------------------------
            # 文本消息
            # -------------------------
            if msg_type in ["message", "text", "barge_in_commit"]:
                user_text = payload.get(
                    "content",
                    payload.get("data", "")
                ).strip()

                incoming_name = (payload.get("userName") or "").strip()
                if incoming_name:
                    current_user_name = incoming_name

                incoming_uid = (payload.get("userId") or "").strip()
                if incoming_uid:
                    current_user_id = incoming_uid

                # 每条消息都带城市，确保天气查询始终用正确定位
                incoming_city = (payload.get("city") or "").strip()
                if incoming_city:
                    current_city = incoming_city

                if user_text:
                    await handle_user_text(user_text)
                continue

            # -------------------------
            # 音频消息
            # 正确用法：用户说话的音频 → audio2face_model → 数字人聆听表情
            # -------------------------
            if msg_type == "audio":
                try:
                    if current_chat_task and not current_chat_task.done():
                        await send_stop_output(websocket, current_response_id + 1, reason="audio_barge_in")

                    audio_data = payload.get("data")

                    if isinstance(audio_data, list):
                        audio_bytes = pcm_list_to_wav_bytes(audio_data)

                    elif isinstance(audio_data, str) and audio_data.startswith("data:audio"):
                        audio_b64 = audio_data.split(",")[1]
                        audio_bytes = base64.b64decode(audio_b64)

                    else:
                        continue

                    loop = asyncio.get_running_loop()

                    # 用户音频 → SER 主情感（陪伴式共情）与 ASR 并行
                    emotion_future = loop.run_in_executor(
                        None,
                        audio_bytes_to_user_emotion,
                        audio_bytes
                    )

                    # ASR 识别用户文本（与情感识别并行）
                    user_text = await loop.run_in_executor(
                        None,
                        asr_engine.speech_to_text,
                        audio_bytes
                    )

                    # 等待 SER，推送主情感给前端，让数字人在 LLM 思考间隙做共情表情
                    user_emotion_label = await emotion_future
                    if user_emotion_label:
                        await safe_send(websocket, {
                            "type": "user_emotion",
                            "emotion": user_emotion_label,
                        })

                    if user_text:
                        await handle_user_text(user_text)

                except Exception as e:
                    print("语音识别失败:", e)
                    await safe_send(websocket, {
                        "type": "error",
                        "message": "语音识别失败"
                    })
                    await safe_send(websocket, {"type": "turn_end", "responseId": current_response_id})

                continue

    except WebSocketDisconnect:
        print("客户端主动断开")

    except RuntimeError as e:
        print("连接关闭:", e)

    except Exception as e:
        print("WebSocket总异常:", e)
        traceback.print_exc()

    finally:
        for t in tasks.copy():
            t.cancel()

        print("连接结束")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
