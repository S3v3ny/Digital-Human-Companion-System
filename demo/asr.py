import torch
try:
    import torch.distributed.tensor  # peft 0.19.x 需要此子模块已被导入
except ImportError:
    pass
import speech_recognition as sr
import numpy as np
from transformers import WhisperProcessor, WhisperForConditionalGeneration
from peft import PeftModel
import os

class CustomASR:
    def __init__(self, 
                 base_model_path="./models/whisper-small-model", # 这里改成你本地存放 whisper-small 基础模型的路径
                 lora_model_path="./whisper-final-model"):  # 这里是你微调保存后的本地文件夹
        
        print(f"正在从本地加载模型...")
        
        # 1. 加载处理器 (从微调后的目录加载，确保 tokenizer 匹配)
        self.processor = WhisperProcessor.from_pretrained(
            base_model_path, 
            local_files_only=True # 同样加上这个，确保它只读本地不连网
        )
        
        # 2. 加载基础模型 (指向本地路径，避免联网检查)
        base_model = WhisperForConditionalGeneration.from_pretrained(
            base_model_path, 
            device_map="auto",
            local_files_only=True  # 强制只读取本地文件
        )
        
        # 3. 挂载本地 LoRA 权重
        print(f"正在挂载本地 LoRA 适配器: {lora_model_path}")
        self.model = PeftModel.from_pretrained(base_model, lora_model_path)
        
        # 切换到推理模式
        self.model.eval()
        self.recognizer = sr.Recognizer()

    def speech_to_text(self, audio_bytes: bytes = None):
        """
        支持两种调用方式：
          1. speech_to_text(audio_bytes)  — server.py WebSocket 模式，传入 WAV/PCM 字节
          2. speech_to_text()             — 独立运行模式，从麦克风录音
        """
        if audio_bytes is not None:
            # ── WebSocket 模式：直接解析传入的音频字节 ──
            try:
                print("[ASR] 正在本地识别（字节流模式）...")
                audio_np = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
            except Exception as e:
                return f"音频解析失败: {e}"
        else:
            # ── 麦克风模式（独立运行时使用）──
            with sr.Microphone() as source:
                print("[ASR] 系统已就绪，请说话...")
                self.recognizer.adjust_for_ambient_noise(source, duration=0.8)
                audio = self.recognizer.listen(source)
            wav_data = audio.get_raw_data(convert_rate=16000, convert_width=2)
            audio_np = np.frombuffer(wav_data, dtype=np.int16).astype(np.float32) / 32768.0

        try:
            print("[ASR] 正在本地识别...")
            
            # 提取特征
            input_features = self.processor(
                audio_np, 
                sampling_rate=16000, 
                return_tensors="pt"
            ).input_features.to("cuda" if torch.cuda.is_available() else "cpu")

            # 本地推理生成
            with torch.no_grad():
                predicted_ids = self.model.generate(
                    input_features, 
                    language="chinese", 
                    max_new_tokens=128
                )
            
            # 解码结果
            transcription = self.processor.batch_decode(predicted_ids, skip_special_tokens=True)[0]
            return transcription

        except Exception as e:
            return f"识别过程出错: {e}"

# --- 实例化引擎 ---
# 注意：第一次运行前，请确保 base_model_path 路径下确实有 whisper-small 的本地文件
# 如果没有，就把 base_model_path 改回 "openai/whisper-small"，它会自动下一次然后缓存
asr_engine = CustomASR(
    base_model_path="./models/whisper-small-model", # 也可以直接写本地 D 盘路径
    lora_model_path="./whisper-final-model"
)

if __name__ == "__main__":
    while True:
        result = asr_engine.speech_to_text()
        print(f"📝 识别结果: {result}")
        print("-" * 30)