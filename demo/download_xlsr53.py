"""
下载 wav2vec2-large-xlsr-53 模型到本地目录
使用 HuggingFace 国内镜像 (hf-mirror.com) 避免 SSL/代理问题

用法:
    python download_xlsr53.py

下载完成后，模型将保存在 ./wav2vec2_xlsr53_local/ 目录中，
Audio2FaceModel 会自动从该目录加载，无需再联网。
"""

import os
import sys

# 设置 HuggingFace 镜像（必须在 import transformers 之前设置）
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

# 禁用 SSL 验证（某些网络环境需要）
os.environ["CURL_CA_BUNDLE"] = ""
os.environ["REQUESTS_CA_BUNDLE"] = ""

import ssl
try:
    _create_unverified_https_context = ssl._create_unverified_context
    ssl._create_default_https_context = _create_unverified_https_context
except AttributeError:
    pass

from transformers import Wav2Vec2Model, Wav2Vec2FeatureExtractor

REPO_ID = "facebook/wav2vec2-large-xlsr-53"
LOCAL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wav2vec2_xlsr53_local")


def main():
    print("=" * 60)
    print("下载 wav2vec2-large-xlsr-53 模型")
    print("镜像源: %s" % os.environ.get("HF_ENDPOINT", "默认"))
    print("保存路径: %s" % LOCAL_DIR)
    print("=" * 60)

    os.makedirs(LOCAL_DIR, exist_ok=True)

    print("\n[1/2] 下载模型权重...")
    try:
        model = Wav2Vec2Model.from_pretrained(REPO_ID)
        model.save_pretrained(LOCAL_DIR)
        print("[OK] 模型权重下载完成")
    except Exception as e:
        print("[ERROR] 模型下载失败: %s" % e)
        print("\n[TIP] 排障建议:")
        print("  1. 检查网络连接")
        print("  2. 尝试使用 VPN 或代理")
        print("  3. 手动从以下地址下载:")
        print("     https://hf-mirror.com/%s" % REPO_ID)
        print("  4. 将下载的文件放入: %s/" % LOCAL_DIR)
        print("     需要的文件: config.json, pytorch_model.bin (或 model.safetensors)")
        sys.exit(1)

    print("\n[2/2] 下载特征提取器配置...")
    try:
        feature_extractor = Wav2Vec2FeatureExtractor.from_pretrained(REPO_ID)
        feature_extractor.save_pretrained(LOCAL_DIR)
        print("[OK] 特征提取器下载完成")
    except Exception as e:
        print("[WARN] 特征提取器下载失败 (非致命): %s" % e)
        print("  模型仍可正常使用，仅缺少 preprocessor_config.json")

    print("\n" + "=" * 60)
    print("[OK] 全部下载完成!")
    print("模型文件位于: %s" % LOCAL_DIR)
    print("\n现在可以运行服务器了:")
    print("  python server.py")
    print("=" * 60)

    # 验证文件
    print("\n已下载的文件:")
    for f in sorted(os.listdir(LOCAL_DIR)):
        size = os.path.getsize(os.path.join(LOCAL_DIR, f))
        if size > 1024 * 1024:
            print("   %s (%.1f MB)" % (f, size / 1024 / 1024))
        else:
            print("   %s (%.1f KB)" % (f, size / 1024))


if __name__ == "__main__":
    main()
