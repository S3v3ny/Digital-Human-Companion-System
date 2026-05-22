"""
下载项目所需的全部 HuggingFace 模型到本地

模型列表：
  1. openai/whisper-small         → ./models/whisper-small-model/
  2. xmj2002/hubert-base-ch-speech-emotion-recognition → ./models/ser-model/
  3. BAAI/bge-small-zh-v1.5       → ./models/bge-small-zh-v1.5/

用法：
    python download_models.py
"""

import os
import sys

# 使用 HuggingFace 国内镜像
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
os.environ["CURL_CA_BUNDLE"] = ""
os.environ["REQUESTS_CA_BUNDLE"] = ""

import ssl
try:
    ssl._create_default_https_context = ssl._create_unverified_context
except AttributeError:
    pass

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(BASE_DIR, "models")
os.makedirs(MODELS_DIR, exist_ok=True)


def banner(title):
    print("\n" + "=" * 60)
    print(f"  {title}")
    print("=" * 60)


def download_whisper_small():
    banner("[1/3] 下载 openai/whisper-small（ASR 基础模型）")
    from transformers import WhisperProcessor, WhisperForConditionalGeneration

    save_dir = os.path.join(MODELS_DIR, "whisper-small-model")
    os.makedirs(save_dir, exist_ok=True)

    print(f"保存路径: {save_dir}")
    print("下载处理器...")
    processor = WhisperProcessor.from_pretrained("openai/whisper-small")
    processor.save_pretrained(save_dir)
    print("[OK] 处理器下载完成")

    print("下载模型权重（约 461 MB）...")
    model = WhisperForConditionalGeneration.from_pretrained("openai/whisper-small")
    model.save_pretrained(save_dir)
    print(f"[OK] whisper-small 下载完成 → {save_dir}")


def download_ser_model():
    banner("[2/3] 下载 xmj2002/hubert-base-ch-speech-emotion-recognition（中文语音情感识别）")
    from transformers import AutoModelForAudioClassification, AutoFeatureExtractor

    repo_id = "xmj2002/hubert-base-ch-speech-emotion-recognition"
    save_dir = os.path.join(MODELS_DIR, "ser-model")
    os.makedirs(save_dir, exist_ok=True)

    print(f"保存路径: {save_dir}")
    print("下载特征提取器配置...")
    feature_extractor = AutoFeatureExtractor.from_pretrained(repo_id)
    feature_extractor.save_pretrained(save_dir)
    print("[OK] 特征提取器下载完成")

    print("下载模型权重（约 370 MB）...")
    model = AutoModelForAudioClassification.from_pretrained(repo_id)
    model.save_pretrained(save_dir)
    print(f"[OK] SER 模型下载完成 → {save_dir}")


def download_bge_model():
    banner("[3/3] 下载 BAAI/bge-small-zh-v1.5（知识库向量模型）")
    from sentence_transformers import SentenceTransformer

    save_dir = os.path.join(MODELS_DIR, "bge-small-zh-v1.5")
    os.makedirs(save_dir, exist_ok=True)

    print(f"保存路径: {save_dir}")
    print("下载模型（约 95 MB）...")
    model = SentenceTransformer("BAAI/bge-small-zh-v1.5")
    model.save(save_dir)
    print(f"[OK] BGE 模型下载完成 → {save_dir}")


def print_summary():
    banner("下载完成汇总")
    models_dir = MODELS_DIR
    for name in ["whisper-small-model", "ser-model", "bge-small-zh-v1.5"]:
        path = os.path.join(models_dir, name)
        if os.path.isdir(path):
            files = os.listdir(path)
            total_mb = sum(
                os.path.getsize(os.path.join(path, f)) / 1024 / 1024
                for f in files
                if os.path.isfile(os.path.join(path, f))
            )
            print(f"  [OK] {name}/ — {len(files)} 个文件，共 {total_mb:.1f} MB")
        else:
            print(f"  [MISS] {name}/ — 未找到，请检查下载是否成功")

    print("\n后续步骤：")
    print("  1. 确认 asr.py 中 base_model_path='./models/whisper-small-model'")
    print("  2. 运行 build_kb.py 生成知识库（需要 psy_data.json）")
    print("  3. 运行 server.py 启动项目")


def main():
    steps = {
        "1": download_whisper_small,
        "2": download_ser_model,
        "3": download_bge_model,
    }

    failed = []
    for key, fn in steps.items():
        try:
            fn()
        except Exception as e:
            print(f"\n[ERROR] 步骤 {key} 失败: {e}")
            import traceback
            traceback.print_exc()
            failed.append(key)

    print_summary()
    if failed:
        print(f"\n[WARN] 以下步骤失败，请手动重试: {failed}")
        sys.exit(1)
    else:
        print("\n[OK] 全部模型下载完成！")


if __name__ == "__main__":
    main()
