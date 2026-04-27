"""
faceformer_adapter.py - Audio2FaceModel 适配器

模型语义说明：
  audio2face_model.pth 的训练任务是：
    给定说话者 (Speaker) 的音频 → 预测听者 (Listener) 的面部情绪反应

正确使用场景：
  ✅ 用户说话时  → 把用户麦克风音频喂给本模型 → 输出数字人的聆听表情序列
  ❌ 数字人说话时 → 不应把 TTS 音频喂给此模型（它不是 Lip-sync 模型）

数字人说话时的口型同步（Lip-sync）需要专门方案，
接口已在 audio_bytes_to_lipsync() 中预留，当前返回 None。
"""

import io
import torch
import librosa
import numpy as np
import soundfile as sf

from model import Audio2FaceModel

# ============================================
# 设备选择
# ============================================
device = "cuda" if torch.cuda.is_available() else "cpu"

# ============================================
# 加载 Audio2FaceModel
# ============================================
print("正在加载 Audio2FaceModel 聆听反应模型...")

audio2face_model = Audio2FaceModel(
    hidden_dim=256,
    emotion_dim=25,
    fv_dim=58,
    num_layers=4,
    nhead=8,
).to(device)

# 加载训练好的权重文件
checkpoint = torch.load("audio2face_model.pth", map_location=device)

# 兼容处理：权重可能直接是 state_dict，也可能包在 checkpoint 字典里
if isinstance(checkpoint, dict) and "model_state_dict" in checkpoint:
    state_dict = checkpoint["model_state_dict"]
elif isinstance(checkpoint, dict) and "state_dict" in checkpoint:
    state_dict = checkpoint["state_dict"]
else:
    state_dict = checkpoint

# 兼容清理 torch.compile() 编译时产生的 '_orig_mod.' 前缀
state_dict = {k.replace('_orig_mod.', ''): v for k, v in state_dict.items()}

audio2face_model.load_state_dict(state_dict, strict=False)
audio2face_model.eval()
print("[OK] Audio2FaceModel 聆听反应模型加载完成")


def _decode_audio(audio_bytes: bytes):
    """
    将音频字节流解码为 16kHz 单声道 numpy 数组。
    优先使用 soundfile（wav/flac），失败则用 librosa（mp3 等）。
    """
    try:
        audio_data, sr = sf.read(io.BytesIO(audio_bytes))
    except Exception:
        import tempfile, os
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        audio_data, sr = librosa.load(tmp_path, sr=16000)
        os.unlink(tmp_path)

    # 多声道取第一声道
    if audio_data.ndim > 1:
        audio_data = audio_data[:, 0]

    # 重采样到 16000Hz
    if sr != 16000:
        audio_data = librosa.resample(audio_data, orig_sr=sr, target_sr=16000)

    return audio_data


def audio_bytes_to_listener_reaction(audio_bytes: bytes) -> list:
    """
    【正确用途】接收用户说话的音频字节流，
    预测数字人（听者 Listener）的面部聆听反应序列。

    模型语义：Speaker音频 → Listener面部反应（点头、微笑、皱眉等）
    输出帧率：25 FPS

    Args:
        audio_bytes: 用户麦克风录制的 WAV/PCM 音频字节

    Returns:
        list[dict]: 逐帧聆听反应列表，每帧格式：
            {
                "emotion": [float, ...],  # 25维情绪特征（AU等，辅助/调试用）
                "fv":      [float, ...],  # 58维面部驱动（前52维=ARKit blendshape，后6维=头部姿态）
            }
        前端主要使用 fv 58维驱动 morphTarget。出错时返回空列表 []。
    """
    try:
        audio_data = _decode_audio(audio_bytes)

        # 转为 PyTorch 张量 [1, num_samples]
        audio_tensor = torch.FloatTensor(audio_data).unsqueeze(0).to(device)

        # 模型推理（模型内部自带 wav2vec2 编码器）
        with torch.no_grad():
            emotion_pred, fv_pred = audio2face_model(audio_tensor)

        # emotion_pred: [1, seq_len, 25] — 辅助情绪特征
        # fv_pred:      [1, seq_len, 58] — 主驱动（ARKit 52 blendshape + 6 头部姿态）
        emotion_np = emotion_pred.squeeze(0).cpu().numpy()  # [seq_len, 25]
        fv_np = fv_pred.squeeze(0).cpu().numpy()            # [seq_len, 58]

        result = []
        for frame_idx in range(fv_np.shape[0]):
            result.append({
                "emotion": emotion_np[frame_idx].tolist(),  # 25维聆听情绪（辅助）
                "fv":      fv_np[frame_idx].tolist(),       # 58维面部驱动（前端主用）
            })

        print(f"[listener_reaction] 生成 {len(result)} 帧聆听反应 (25fps)")
        return result

    except Exception as e:
        print(f"[listener_reaction] 计算失败: {e}")
        import traceback
        traceback.print_exc()
        return []


def audio_bytes_to_lipsync(audio_bytes: bytes):
    """
    【接口预留】数字人说话时的口型同步驱动。

    当前状态：返回 None（不驱动口型）。
    TODO: 接入专门的 Lip-sync 方案后替换此函数，例如：
        - MuseTalk
        - FaceFormer（原始口型任务版本）
        - edge-tts SSML 音素时序直接驱动

    Args:
        audio_bytes: 数字人 TTS 生成的音频字节

    Returns:
        None（当前未实现），未来返回口型帧列表
    """
    # TODO: 实现专门的 Lip-sync 方案
    return None
