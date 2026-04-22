"""
Audio2FaceModel - 基于 Wav2Vec2 (XLSR-53) + Transformer 的面部行为驱动模型
输出:
  - emotion_head: [batch, seq_len, 25] (15 AU + 2 VA + 8 EXP，辅助/评测用)
  - fv_head:      [batch, seq_len, 58] (前52维=ARKit blendshape，后6维=头部姿态，主驱动)

前端聆听反应动画使用 fv 58维直接驱动 Three.js morphTarget。

注意: 此架构已更新为 Transformer 版本，与 audio2face_model.pth 权重文件匹配。
      旧版 LSTM 架构不再兼容。
"""

import os
import math
import torch
import torch.nn as nn
from transformers import Wav2Vec2Model

# ============================================
# HuggingFace 镜像配置（解决国内网络下载问题）
# 优先级：环境变量 > 国内镜像 > 官方源
# ============================================
if "HF_ENDPOINT" not in os.environ:
    os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

# XLSR-53 模型标识符（用于在线下载）
_XLSR53_REPO = "facebook/wav2vec2-large-xlsr-53"

# 本地缓存目录（优先从此处加载，避免每次联网）
_XLSR53_LOCAL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wav2vec2_xlsr53_local")


def _load_wav2vec2_encoder():
    """
    加载 wav2vec2-large-xlsr-53 编码器。
    加载优先级：
      1) 本地目录 from_pretrained（新版 transformers 支持 safetensors）
      2) 本地目录 手动加载 safetensors（旧版 transformers 兼容）
      3) 在线下载（HuggingFace 镜像）
    """
    # 1) 尝试从本地目录加载
    if os.path.isdir(_XLSR53_LOCAL_DIR) and os.path.isfile(
        os.path.join(_XLSR53_LOCAL_DIR, "config.json")
    ):
        try:
            print(f"[LOCAL] 从本地目录加载 XLSR-53: {_XLSR53_LOCAL_DIR}")
            model = Wav2Vec2Model.from_pretrained(_XLSR53_LOCAL_DIR)
            print("[OK] XLSR-53 本地加载成功")
            return model
        except Exception as e:
            print(f"[WARN] 本地加载失败: {e}")

    # 1.5) 如果本地有 model.safetensors 但 from_pretrained 不认识，手动加载
    _safetensors_path = os.path.join(_XLSR53_LOCAL_DIR, "model.safetensors")
    _config_path = os.path.join(_XLSR53_LOCAL_DIR, "config.json")
    if os.path.isfile(_safetensors_path) and os.path.isfile(_config_path):
        try:
            print("[LOCAL] 尝试手动加载 safetensors 格式权重...")
            from transformers import Wav2Vec2Config
            # 用 numpy 后端加载，兼容旧版 PyTorch（无 torch.frombuffer）
            from safetensors.numpy import load_file as np_load_file
            import numpy as _np

            config = Wav2Vec2Config.from_pretrained(_XLSR53_LOCAL_DIR)
            np_state = np_load_file(_safetensors_path)
            state_dict = {k: torch.from_numpy(v.copy()) for k, v in np_state.items()}
            model = Wav2Vec2Model(config)
            model.load_state_dict(state_dict, strict=False)
            print("[OK] XLSR-53 通过 safetensors 手动加载成功")
            return model
        except Exception as e2:
            print(f"[WARN] safetensors 手动加载也失败: {e2}, 尝试在线下载...")

    # 2) 从 HuggingFace（镜像）在线下载
    try:
        print(f"[NET] 从 {os.environ.get('HF_ENDPOINT', 'huggingface.co')} 下载 XLSR-53...")
        model = Wav2Vec2Model.from_pretrained(_XLSR53_REPO)
        print("[OK] XLSR-53 在线下载成功")

        # 自动保存到本地目录，下次直接本地加载
        try:
            model.save_pretrained(_XLSR53_LOCAL_DIR)
            print(f"[SAVE] 已保存到本地: {_XLSR53_LOCAL_DIR}")
        except Exception as save_err:
            print(f"[WARN] 保存本地缓存失败: {save_err}")

        return model
    except Exception as e:
        print(f"[ERROR] XLSR-53 加载失败: {e}")
        print("[TIP] 解决方案:")
        print(f"   1. 手动运行: python download_xlsr53.py")
        print(f"   2. 设置镜像: set HF_ENDPOINT=https://hf-mirror.com")
        print(f"   3. 手动下载模型文件到: {_XLSR53_LOCAL_DIR}/")
        raise


class CCCLoss(nn.Module):
    """Concordance Correlation Coefficient Loss"""
    def __init__(self):
        super(CCCLoss, self).__init__()

    def forward(self, pred, target):
        pred_mean = torch.mean(pred, dim=1, keepdim=True)
        target_mean = torch.mean(target, dim=1, keepdim=True)
        pred_var = torch.var(pred, dim=1, keepdim=True)
        target_var = torch.var(target, dim=1, keepdim=True)
        covariance = torch.mean(
            (pred - pred_mean) * (target - target_mean), dim=1, keepdim=True
        )
        ccc = (2.0 * covariance) / (
            pred_var + target_var + (pred_mean - target_mean) ** 2 + 1e-8
        )
        return 1.0 - torch.mean(ccc)


class PositionalEncoding(nn.Module):
    """Transformer 位置编码"""
    def __init__(self, d_model, max_len=5000):
        super().__init__()
        position = torch.arange(max_len).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2) * (-math.log(10000.0) / d_model))
        pe = torch.zeros(max_len, 1, d_model)
        pe[:, 0, 0::2] = torch.sin(position * div_term)
        pe[:, 0, 1::2] = torch.cos(position * div_term)
        self.register_buffer('pe', pe)

    def forward(self, x):
        # x: [batch, seq_len, d_model]
        x = x + self.pe[:x.size(1)].transpose(0, 1)
        return x


class Audio2FaceModel(nn.Module):
    def __init__(self, hidden_dim=256, emotion_dim=25, fv_dim=58, num_layers=4, nhead=8):
        super(Audio2FaceModel, self).__init__()

        # 使用跨语言大模型 XLSR-53（输出维度 1024）
        self.audio_encoder = _load_wav2vec2_encoder()

        # 冻结音频编码器的所有参数以加速训练/推理
        for param in self.audio_encoder.parameters():
            param.requires_grad = False

        # 时间对齐层：将 1024 维音频特征降维 + 降采样
        self.time_align = nn.Sequential(
            nn.Conv1d(1024, hidden_dim, kernel_size=3, stride=2, padding=1),
            nn.BatchNorm1d(hidden_dim),
            nn.GELU(),
        )

        # 位置编码
        self.pos_encoder = PositionalEncoding(d_model=hidden_dim)

        # Transformer 编码器（替代旧版 LSTM）
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=hidden_dim,
            nhead=nhead,
            dim_feedforward=hidden_dim * 4,
            dropout=0.1,
            activation='gelu',
            batch_first=True,
        )
        self.transformer = nn.TransformerEncoder(encoder_layer, num_layers=num_layers)

        # 双输出头（注意：Transformer 不是双向的，所以输入维度是 hidden_dim 而非 hidden_dim*2）
        self.emotion_head = nn.Linear(hidden_dim, emotion_dim)  # 25 维情绪
        self.fv_head = nn.Linear(hidden_dim, fv_dim)            # 58 维口型

    def forward(self, waveform):
        """
        Args:
            waveform: [batch, num_samples] 原始 16kHz 音频波形
        Returns:
            emotion: [batch, seq_len, 25]
            fv:      [batch, seq_len, 58]
        """
        with torch.no_grad():
            audio_outputs = self.audio_encoder(waveform).last_hidden_state
        # audio_outputs: [batch, time_steps, 1024]

        x = audio_outputs.transpose(1, 2)   # -> [batch, 1024, time_steps]
        x = self.time_align(x)              # -> [batch, hidden_dim, time_steps//2]
        x = x.transpose(1, 2)              # -> [batch, time_steps//2, hidden_dim]

        x = self.pos_encoder(x)            # 添加位置编码
        x = self.transformer(x)            # -> [batch, time_steps//2, hidden_dim]

        emotion = self.emotion_head(x)      # -> [batch, seq_len, 25]
        fv = self.fv_head(x)                # -> [batch, seq_len, 58]

        return emotion, fv
