"""
faceformer_adapter.py — 中文语音情感识别驱动的数字人表情

流程：
  用户音频 → xmj2002/hubert-base-ch-speech-emotion-recognition
           → 情感概率分布（happy/sad/angry/surprise/…）
           → 混合 ARKit blendshape 目标权重
           → 帧级能量动态 + 自然眨眼
           → 25 FPS 逐帧 fv 序列

输出格式与原 audio2face_model 完全兼容：
  list[{"emotion": [...25], "fv": [...58]}]
"""

import io
import os
import math
import random
import numpy as np
import librosa
import soundfile as sf

# HuggingFace 镜像（在 import transformers 之前设置）
if "HF_ENDPOINT" not in os.environ:
    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

# ── 常量 ──────────────────────────────────────────────────────────────────────
FPS = 25
SR  = 16000
HOP = SR // FPS          # 640 samples/frame @ 25fps

SER_MODEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models", "ser-model")

# ── SER 模型懒加载 ─────────────────────────────────────────────────────────────
_ser_pipeline = None

def _get_ser_pipeline():
    global _ser_pipeline
    if _ser_pipeline is not None:
        return _ser_pipeline
    from transformers import pipeline
    print(f"[SER] 正在加载中文语音情感识别模型: {SER_MODEL}")
    _ser_pipeline = pipeline(
        "audio-classification",
        model=SER_MODEL,
        device=-1,          # CPU 推理，避免与 wav2vec2 争 GPU 显存
        top_k=None,         # 返回全部情感概率
    )
    print("[SER] 模型加载完成")
    return _ser_pipeline


# ── ARKit blendshape 情感模板 ─────────────────────────────────────────────────
# 索引对应 script.js 中 ARKIT_BLENDSHAPE_NAMES（0~51 = ARKit 52维）
# 每个情感对应目标权重，其余索引默认为 0
EMOTION_BLENDSHAPES = {
    "neutral": {
        23: 0.06, 24: 0.06,         # mouthSmileLeft/Right  淡淡微笑
        43: 0.04,                   # browInnerUp            轻微放松
    },
    "happy": {
        23: 0.55, 24: 0.55,         # mouthSmileLeft/Right
        47: 0.30, 48: 0.30,         # cheekSquintLeft/Right
        5:  0.20, 12: 0.20,         # eyeSquintLeft/Right
        43: 0.15,                   # browInnerUp
        44: 0.10, 45: 0.10,         # browOuterUpLeft/Right
        17: 0.08,                   # jawOpen               微张
    },
    "sad": {
        25: 0.35, 26: 0.35,         # mouthFrownLeft/Right
        43: 0.40,                   # browInnerUp           悲伤眉型
        41: 0.20, 42: 0.20,         # browDownLeft/Right
        5:  0.15, 12: 0.15,         # eyeSquintLeft/Right
        33: 0.10,                   # mouthShrugLower
    },
    "angry": {
        41: 0.55, 42: 0.55,         # browDownLeft/Right
        49: 0.30, 50: 0.30,         # noseSneerLeft/Right
        35: 0.25, 36: 0.25,         # mouthPressLeft/Right
        25: 0.15, 26: 0.15,         # mouthFrownLeft/Right
        14: 0.10,                   # jawForward
    },
    "surprise": {
        6:  0.60, 13: 0.60,         # eyeWideLeft/Right
        44: 0.50, 45: 0.50,         # browOuterUpLeft/Right
        43: 0.45,                   # browInnerUp
        17: 0.35,                   # jawOpen
    },
    "fear": {
        6:  0.45, 13: 0.45,         # eyeWideLeft/Right
        44: 0.35, 45: 0.35,         # browOuterUpLeft/Right
        43: 0.30,                   # browInnerUp
        29: 0.20, 30: 0.20,         # mouthStretchLeft/Right
        41: 0.15, 42: 0.15,         # browDownLeft/Right
        17: 0.15,                   # jawOpen
    },
    "disgust": {
        49: 0.45, 50: 0.45,         # noseSneerLeft/Right
        41: 0.30, 42: 0.30,         # browDownLeft/Right
        25: 0.25, 26: 0.25,         # mouthFrownLeft/Right
        21: 0.15,                   # mouthLeft
    },
    # 部分模型可能有以下标签
    "bored": {
        25: 0.20, 26: 0.20,
        5:  0.15, 12: 0.15,
        41: 0.10, 42: 0.10,
    },
    "excited": {
        23: 0.50, 24: 0.50,
        6:  0.25, 13: 0.25,
        44: 0.30, 45: 0.30,
        17: 0.20,
    },
    "frustrated": {
        41: 0.40, 42: 0.40,
        35: 0.20, 36: 0.20,
        49: 0.15, 50: 0.15,
    },
}

# 模型标签别名归一化（不同数据集标签名称不统一）
_LABEL_ALIASES = {
    "ang": "angry",    "anger": "angry",
    "hap": "happy",    "happiness": "happy",   "exc": "excited",
    "sad": "sad",      "sadness": "sad",
    "neu": "neutral",  "neutrality": "neutral", "normal": "neutral",
    "sur": "surprise", "surprised": "surprise",
    "fea": "fear",     "fearful": "fear",
    "dis": "disgust",
    "bor": "bored",
    "fru": "frustrated",
}

def _normalize_label(label: str) -> str:
    label = label.lower().strip()
    return _LABEL_ALIASES.get(label, label)


# ── blendshape 工具 ────────────────────────────────────────────────────────────
def _emotion_probs_to_fv(emotion_probs: dict) -> np.ndarray:
    """将情感概率分布加权混合为 58 维 fv 目标权重。"""
    fv = np.zeros(58, dtype=np.float32)
    for raw_label, prob in emotion_probs.items():
        label = _normalize_label(raw_label)
        template = EMOTION_BLENDSHAPES.get(label, EMOTION_BLENDSHAPES["neutral"])
        for idx, weight in template.items():
            fv[idx] += weight * float(prob)
    return np.clip(fv, 0.0, 1.0)


# ── 自然眨眼曲线 ──────────────────────────────────────────────────────────────
_BLINK_INTERVAL = 100   # ~4 秒眨一次（25fps）
_BLINK_DURATION = 4     # 眨眼持续帧数

def _build_blink_curve(n: int) -> np.ndarray:
    rng   = random.Random(42)
    curve = np.zeros(n, dtype=np.float32)
    i = rng.randint(20, _BLINK_INTERVAL)
    while i < n:
        for d in range(_BLINK_DURATION):
            if i + d < n:
                curve[i + d] = math.sin(d / _BLINK_DURATION * math.pi)
        i += rng.randint(_BLINK_INTERVAL - 20, _BLINK_INTERVAL + 40)
    return curve


# ── 帧生成 ────────────────────────────────────────────────────────────────────
def _build_frames(audio_data: np.ndarray, base_fv: np.ndarray) -> list:
    """
    将情感目标权重 base_fv 展开为逐帧动画序列。
    - 用音频 RMS 能量轻微调节表情幅度（声音越响情绪越强）
    - 自然眨眼覆盖眼部 blendshape
    - EMA 平滑消除帧间抖动
    """
    rms = librosa.feature.rms(
        y=audio_data, frame_length=HOP * 2, hop_length=HOP
    )[0].astype(np.float32)
    n    = len(rms)
    peak = rms.max()
    rms_norm = np.clip(rms / peak, 0.0, 1.0) if peak > 1e-6 else np.zeros(n)

    blink = _build_blink_curve(n)

    EMA_ALPHA = 0.20
    ema    = np.zeros(58, dtype=np.float32)
    frames = []

    for i in range(n):
        e      = float(rms_norm[i])
        target = base_fv.copy()

        # 能量调制：声音响时情绪幅度更大（系数 0.65~1.15）
        target = np.clip(target * (0.65 + e * 0.50), 0.0, 1.0)

        # 覆盖眼部 blendshape 为自然眨眼
        blink_val   = float(blink[i]) * 0.90
        target[0]   = blink_val    # eyeBlinkLeft
        target[7]   = blink_val    # eyeBlinkRight

        # EMA 平滑
        ema = EMA_ALPHA * target + (1.0 - EMA_ALPHA) * ema

        frames.append({
            "emotion": [0.0] * 25,
            "fv":      ema.tolist(),
        })

    return frames


# ── 音频解码 ──────────────────────────────────────────────────────────────────
def _decode_audio(audio_bytes: bytes) -> np.ndarray:
    try:
        audio_data, sr = sf.read(io.BytesIO(audio_bytes))
    except Exception:
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        audio_data, sr = librosa.load(tmp_path, sr=SR)
        os.unlink(tmp_path)
    if audio_data.ndim > 1:
        audio_data = audio_data[:, 0]
    if sr != SR:
        audio_data = librosa.resample(audio_data, orig_sr=sr, target_sr=SR)
    return audio_data.astype(np.float32)


# ── 对外接口 ──────────────────────────────────────────────────────────────────
def audio_bytes_to_listener_reaction(audio_bytes: bytes) -> list:
    """
    接收用户说话的音频字节流，返回数字人表情帧序列（25 FPS）。
    每帧格式：{"emotion": [...25], "fv": [...58]}
    """
    try:
        audio_data = _decode_audio(audio_bytes)

        # 1. 语音情感识别 → 概率分布
        pipe    = _get_ser_pipeline()
        results = pipe({"raw": audio_data, "sampling_rate": SR}, top_k=None)
        emotion_probs = {r["label"]: r["score"] for r in results}
        dominant = max(emotion_probs, key=emotion_probs.get)
        print(f"[SER] 情感识别: { {k: f'{v:.2f}' for k, v in emotion_probs.items()} }")
        print(f"[SER] 主情感: {dominant} ({emotion_probs[dominant]:.2f})")

        # 2. 情感概率 → blendshape 目标权重
        base_fv = _emotion_probs_to_fv(emotion_probs)

        # 3. 帧级动态展开
        frames = _build_frames(audio_data, base_fv)
        print(f"[listener_reaction] 生成 {len(frames)} 帧 (情感驱动, 主情感={dominant})")
        return frames

    except Exception as e:
        import traceback
        print(f"[listener_reaction] SER 推理失败，回退程序化: {e}")
        traceback.print_exc()
        return _fallback(audio_bytes)


def _fallback(audio_bytes: bytes) -> list:
    """SER 模型不可用时的纯能量驱动回退。"""
    try:
        audio_data = _decode_audio(audio_bytes)
        neutral_fv = np.zeros(58, dtype=np.float32)
        for idx, w in EMOTION_BLENDSHAPES["neutral"].items():
            neutral_fv[idx] = w
        return _build_frames(audio_data, neutral_fv)
    except Exception:
        return []


def audio_bytes_to_lipsync(audio_bytes: bytes):
    """口型同步由 Azure TTS viseme 方案处理，此处返回 None。"""
    return None


def audio_bytes_to_user_emotion(audio_bytes: bytes):
    """仅返回 SER 主情感标签（归一化后），用于陪伴式共情表情。"""
    try:
        audio_data = _decode_audio(audio_bytes)
        pipe = _get_ser_pipeline()
        results = pipe({"raw": audio_data, "sampling_rate": SR}, top_k=None)
        dominant = max(results, key=lambda r: r["score"])
        label = _normalize_label(dominant["label"])
        print(f"[SER] user_emotion = {label} ({dominant['score']:.2f})")
        return label
    except Exception as e:
        print(f"[SER] user_emotion 失败: {e}")
        return None
