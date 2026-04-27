"""
generate_predictions.py
=======================
根据官方 person_specific_val.csv，对每个样本用 speaker 音频推理 K=10 次，
生成 prediction_emotion.npy，形状 [N, K, T=750, 25]，供官方评测脚本使用。

用法：
    python generate_predictions.py \
        --data-root  ../官方资料/data \
        --index-csv  ../官方资料/验证集自测包/person_specific_val.csv \
        --weights    audio2face_model.pth \
        --output     prediction_emotion.npy \
        [--split val] [--T 750] [--K 10]
"""

import argparse
import csv
import os
import sys
import io

import numpy as np
import torch
import torchaudio

# 确保能 import demo/model.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from model import Audio2FaceModel


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--data-root",  required=True,  help="数据集根目录，含 train/val 子目录")
    p.add_argument("--index-csv",  required=True,  help="person_specific_val.csv 路径")
    p.add_argument("--weights",    default="audio2face_model.pth", help="模型权重文件")
    p.add_argument("--output",     default="prediction_emotion.npy", help="输出 npy 文件路径")
    p.add_argument("--split",      default="val")
    p.add_argument("--T",          type=int, default=750, help="目标序列长度（帧数）")
    p.add_argument("--K",          type=int, default=10,  help="每样本生成候选数")
    return p.parse_args()


# ──────────────────────────────────────────────
# 读取 CSV，展开为 2N 个 (speaker_path, listener_path)
# ──────────────────────────────────────────────
def load_sample_order(index_csv):
    with open(index_csv, "r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    data_rows = rows[1:]  # 跳过表头
    speaker_paths, listener_paths = [], []
    for row in data_rows:
        speaker_paths.append(row[1].strip())
        listener_paths.append(row[2].strip())
    # 正向 + 反向，与官方 eval 脚本完全一致
    all_speakers  = speaker_paths  + listener_paths
    all_listeners = listener_paths + speaker_paths
    return all_speakers, all_listeners


# ──────────────────────────────────────────────
# video_rel_path → Audio_files/<path>.wav
# ──────────────────────────────────────────────
def video_to_audio_path(data_root, split, video_rel_path):
    rel = video_rel_path.replace("\\", "/")
    audio_rel = f"Audio_files/{rel}.wav"
    return os.path.join(data_root, split, audio_rel)


# ──────────────────────────────────────────────
# 加载音频 → 16kHz 单声道 tensor [1, samples]
# ──────────────────────────────────────────────
def load_audio(audio_path, device):
    waveform, sr = torchaudio.load(audio_path)
    if sr != 16000:
        waveform = torchaudio.transforms.Resample(orig_freq=sr, new_freq=16000)(waveform)
    # 多声道取均值
    waveform = waveform.mean(dim=0, keepdim=True)  # [1, samples]
    return waveform.to(device)


# ──────────────────────────────────────────────
# 对单条序列做 pad/truncate 到目标长度 T
# ──────────────────────────────────────────────
def align_length(seq, T):
    """seq: [seq_len, 25] → [T, 25]"""
    L = seq.shape[0]
    if L >= T:
        return seq[:T]
    pad = np.zeros((T - L, seq.shape[1]), dtype=np.float32)
    return np.concatenate([seq, pad], axis=0)


# ──────────────────────────────────────────────
# 加载模型
# ──────────────────────────────────────────────
def load_model(weights_path, device):
    model = Audio2FaceModel(
        hidden_dim=256, emotion_dim=25, fv_dim=58, num_layers=4, nhead=8
    ).to(device)

    checkpoint = torch.load(weights_path, map_location=device)
    if isinstance(checkpoint, dict) and "model_state_dict" in checkpoint:
        state_dict = checkpoint["model_state_dict"]
    elif isinstance(checkpoint, dict) and "state_dict" in checkpoint:
        state_dict = checkpoint["state_dict"]
    else:
        state_dict = checkpoint

    state_dict = {k.replace("_orig_mod.", ""): v for k, v in state_dict.items()}
    model.load_state_dict(state_dict, strict=False)
    print(f"[OK] 模型权重加载完成: {weights_path}")
    return model


# ──────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────
def main():
    args = parse_args()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    print(f"使用设备: {device}")

    model = load_model(args.weights, device)

    all_speakers, _ = load_sample_order(args.index_csv)
    N = len(all_speakers)
    print(f"评测样本总数 N={N}，K={args.K}，T={args.T}")

    # 结果数组 [N, K, T, 25]
    predictions = np.zeros((N, args.K, args.T, 25), dtype=np.float32)

    for n, speaker_rel in enumerate(all_speakers):
        audio_path = video_to_audio_path(args.data_root, args.split, speaker_rel)

        if not os.path.exists(audio_path):
            print(f"[WARN] 音频文件不存在，用零填充: {audio_path}")
            # predictions[n] 已初始化为 0，跳过
            continue

        try:
            waveform = load_audio(audio_path, device)
        except Exception as e:
            print(f"[WARN] 音频加载失败 ({audio_path}): {e}，用零填充")
            continue

        # K=10 次推理：开启 train 模式激活 MC Dropout 产生多样性
        model.train()
        candidates = []
        with torch.no_grad():
            for k in range(args.K):
                emotion_pred, _ = model(waveform)          # [1, seq_len, 25]
                seq = emotion_pred.squeeze(0).cpu().numpy() # [seq_len, 25]
                seq = align_length(seq, args.T)             # [T, 25]
                candidates.append(seq)

        predictions[n] = np.stack(candidates, axis=0)  # [K, T, 25]

        if (n + 1) % 10 == 0 or (n + 1) == N:
            print(f"  进度: {n+1}/{N}")

    np.save(args.output, predictions)
    print(f"\n[完成] prediction_emotion.npy 已保存: {args.output}")
    print(f"  形状: {predictions.shape}  dtype: {predictions.dtype}")


if __name__ == "__main__":
    main()
