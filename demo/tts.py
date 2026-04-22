"""
Azure AI Speech TTS 引擎
- generate_audio_bytes_with_visemes(): 完整音频 + viseme 口型时间轴
- stream_audio_chunks(): 真正流式 yield 音频块
- _build_ssml(): SSML 模板，支持 mstts:express-as 情感 + prosody 语速/音调
"""

import asyncio
import os
import threading
from textwrap import dedent

import azure.cognitiveservices.speech as speechsdk


class TTSGenerationError(Exception):
    """TTS 生成失败或未返回有效音频。"""


# ── 角色配置 ──────────────────────────────────────────────────────────────────
# voice: Azure Neural 声音名称
# style: mstts:express-as 情感风格（不支持该风格的声音会静默忽略）
# rate / pitch: prosody 参数，相对值如 "+5%" 或 "-3%"
ROLE_CONFIG = {
    "girl": {
        "voice": "zh-CN-XiaoxiaoNeural",
        "style": "friendly",
        "rate": "+5%",
        "pitch": "+2%",
    },
    "boy": {
        "voice": "zh-CN-YunxiNeural",
        "style": "chat",
        "rate": "0%",
        "pitch": "0%",
    },
    "elderly": {
        "voice": "zh-CN-YunzeNeural",
        "style": "calm",
        "rate": "-5%",
        "pitch": "-3%",
    },
}

_SENTINEL = object()  # stream 结束哨兵


# ── 流式回调 ──────────────────────────────────────────────────────────────────
class _PushStreamCallback(speechsdk.audio.PushAudioOutputStreamCallback):
    """
    Azure SDK 在合成线程中调用 write()，通过 call_soon_threadsafe 把数据
    投递到 asyncio.Queue，供 async generator 消费。
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue):
        super().__init__()
        self._loop = loop
        self._queue = queue

    def write(self, audio_buffer: memoryview) -> int:
        data = bytes(audio_buffer)
        if data:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, data)
        return len(audio_buffer)

    def close(self):
        self._loop.call_soon_threadsafe(self._queue.put_nowait, _SENTINEL)


# ── 主引擎 ────────────────────────────────────────────────────────────────────
class AzureTTSEngine:
    def __init__(self):
        key = os.environ.get("AZURE_SPEECH_KEY", "")
        region = os.environ.get("AZURE_SPEECH_REGION", "")
        if not key or not region:
            raise RuntimeError(
                "Azure TTS 需要设置环境变量 AZURE_SPEECH_KEY 和 AZURE_SPEECH_REGION"
            )
        self._key = key
        self._region = region

    # ── SSML 构建 ─────────────────────────────────────────────────────────────
    def _build_ssml(self, text: str, role: str) -> str:
        cfg = ROLE_CONFIG.get(role, ROLE_CONFIG["girl"])
        voice = cfg["voice"]
        style = cfg["style"]
        rate = cfg["rate"]
        pitch = cfg["pitch"]
        # 对文本做基础 XML 转义，防止破坏 SSML 结构
        safe = (
            text.replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
                .replace('"', "&quot;")
        )
        return dedent(f"""\
            <speak version="1.0"
                   xmlns="http://www.w3.org/2001/10/synthesis"
                   xmlns:mstts="http://www.w3.org/2001/mstts"
                   xml:lang="zh-CN">
              <voice name="{voice}">
                <mstts:express-as style="{style}" styledegree="1.5">
                  <prosody rate="{rate}" pitch="{pitch}">
                    {safe}
                  </prosody>
                </mstts:express-as>
              </voice>
            </speak>""")

    # ── 创建 SpeechConfig ─────────────────────────────────────────────────────
    def _make_speech_config(self) -> speechsdk.SpeechConfig:
        cfg = speechsdk.SpeechConfig(subscription=self._key, region=self._region)
        cfg.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Audio16Khz32KBitRateMonoMp3
        )
        return cfg

    # ── 文本规范化 ────────────────────────────────────────────────────────────
    @staticmethod
    def _normalize(text: str) -> str:
        clean = (text or "").strip()
        clean = clean.replace("\u200b", "").replace("\ufeff", "").strip()
        if clean and all(ch in "。，！？,.!?、；;：:~～…" for ch in clean):
            return ""
        return clean

    # ── 完整合成 + viseme ─────────────────────────────────────────────────────
    async def generate_audio_bytes_with_visemes(
        self, text: str, role: str = "girl"
    ) -> tuple[bytes, list[dict]]:
        """
        返回 (audio_bytes: bytes, visemes: list[{"time_ms": int, "viseme_id": int}])
        audio_offset 单位为 100 纳秒，转换：time_ms = offset // 10_000
        """
        clean = self._normalize(text)
        if not clean:
            raise TTSGenerationError("TTS 输入文本为空或仅包含标点")

        ssml = self._build_ssml(clean, role)
        visemes: list[dict] = []

        def _run_sync() -> bytes:
            speech_cfg = self._make_speech_config()
            synthesizer = speechsdk.SpeechSynthesizer(
                speech_config=speech_cfg, audio_config=None
            )

            def _on_viseme(evt: speechsdk.SpeechSynthesisVisemeEventArgs):
                visemes.append({
                    "time_ms": int(evt.audio_offset) // 10_000,
                    "viseme_id": int(evt.viseme_id),
                })

            synthesizer.viseme_received.connect(_on_viseme)
            result: speechsdk.SpeechSynthesisResult = (
                synthesizer.speak_ssml_async(ssml).get()
            )

            if result.reason == speechsdk.ResultReason.SynthesizingAudioCompleted:
                return result.audio_data

            details = speechsdk.CancellationDetails.from_result(result)
            raise TTSGenerationError(
                f"Azure TTS 合成失败: reason={result.reason}, "
                f"error_code={details.error_code}, detail={details.error_details}"
            )

        loop = asyncio.get_event_loop()
        try:
            audio_bytes: bytes = await asyncio.wait_for(
                loop.run_in_executor(None, _run_sync),
                timeout=30,
            )
        except asyncio.TimeoutError:
            raise TTSGenerationError("Azure TTS 合成超时（30s）")

        if not audio_bytes:
            raise TTSGenerationError("Azure TTS 返回空音频")

        print(
            f"-> Azure TTS+Viseme 完成: role={role}, "
            f"音频={len(audio_bytes)}B, viseme帧={len(visemes)}"
        )
        return audio_bytes, visemes

    # ── 流式合成 ──────────────────────────────────────────────────────────────
    async def stream_audio_chunks(self, text: str, role: str = "girl"):
        """
        真正流式 async generator，每次 yield 一块 MP3 字节。
        Azure SDK 在合成线程中通过 PushAudioOutputStream 回调推送数据，
        经 asyncio.Queue 桥接到 async generator。
        """
        clean = self._normalize(text)
        if not clean:
            raise TTSGenerationError("TTS 输入文本为空或仅包含标点")

        ssml = self._build_ssml(clean, role)
        loop = asyncio.get_event_loop()
        queue: asyncio.Queue = asyncio.Queue()
        error_holder: list[Exception] = []

        callback = _PushStreamCallback(loop, queue)
        push_stream = speechsdk.audio.PushAudioOutputStream(callback)
        audio_cfg = speechsdk.audio.AudioOutputConfig(stream=push_stream)

        def _run_sync():
            try:
                speech_cfg = self._make_speech_config()
                synthesizer = speechsdk.SpeechSynthesizer(
                    speech_config=speech_cfg, audio_config=audio_cfg
                )
                result: speechsdk.SpeechSynthesisResult = (
                    synthesizer.speak_ssml_async(ssml).get()
                )
                if result.reason != speechsdk.ResultReason.SynthesizingAudioCompleted:
                    details = speechsdk.CancellationDetails.from_result(result)
                    err = TTSGenerationError(
                        f"Azure TTS 流式合成失败: reason={result.reason}, "
                        f"error_code={details.error_code}, detail={details.error_details}"
                    )
                    error_holder.append(err)
                    loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL)
            except Exception as e:
                error_holder.append(e)
                loop.call_soon_threadsafe(queue.put_nowait, _SENTINEL)

        # 在线程池中运行同步 SDK，不阻塞 event loop
        sdk_task = loop.run_in_executor(None, _run_sync)

        got_audio = False
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=30)
                except asyncio.TimeoutError:
                    raise TTSGenerationError("Azure TTS 流式合成超时（30s）")

                if item is _SENTINEL:
                    break
                got_audio = True
                yield item
        finally:
            # 确保 SDK 线程任务被等待，避免悬空
            try:
                await asyncio.wait_for(sdk_task, timeout=5)
            except Exception:
                pass

        if error_holder:
            raise TTSGenerationError(str(error_holder[0])) from error_holder[0]

        if not got_audio:
            raise TTSGenerationError("Azure TTS 流式合成返回空音频")

    # ── 兼容旧接口（server.py 未使用，但保留以防其他调用方） ──────────────────
    async def generate_audio_bytes(self, text: str, role: str = "girl") -> bytes:
        audio_bytes, _ = await self.generate_audio_bytes_with_visemes(text, role)
        return audio_bytes


# 全局单例，server.py 通过 `from tts import tts_engine, TTSGenerationError` 使用
tts_engine = AzureTTSEngine()
