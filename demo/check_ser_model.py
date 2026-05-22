"""
运行此脚本可查看 SER 模型的情感标签及一次示例推理结果。
用法：python check_ser_model.py
"""
import os, numpy as np

os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

MODEL = "xmj2002/hubert-base-ch-speech-emotion-recognition"

print("=" * 55)
print(f"加载模型: {MODEL}")
print("=" * 55)

from transformers import AutoConfig, pipeline

# 1. 直接读 config，获取 id2label
cfg = AutoConfig.from_pretrained(MODEL)
print("\n【情感标签列表】")
for idx, label in cfg.id2label.items():
    print(f"  {idx}: {label}")

# 2. 用 1 秒静音音频做一次推理，确认输出格式
print("\n【推理格式测试（1秒静音）】")
pipe = pipeline("audio-classification", model=MODEL, device=-1, top_k=None)
dummy = np.zeros(16000, dtype=np.float32)
results = pipe({"raw": dummy, "sampling_rate": 16000}, top_k=None)
for r in sorted(results, key=lambda x: -x["score"]):
    print(f"  {r['label']:<15} {r['score']:.4f}")

print("\n完成，以上即为该模型可识别的全部情感类别。")
