# ============================================================
# 情感陪护虚拟数字人系统 - Docker 镜像
# ============================================================
# 构建命令：
#   docker build -t digital-human .
#
# 运行命令（需要 NVIDIA Container Toolkit）：
#   docker run --gpus all -p 8000:8000 digital-human
#
# 或使用 docker-compose（推荐）：
#   docker compose up
# ============================================================

# 使用官方 miniconda3（基于 debian，体积最小的 conda 基础镜像）
FROM continuumio/miniconda3:latest

WORKDIR /app

# ── 基础环境变量 ────────────────────────────────────────────
ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
# HuggingFace 镜像（构建时下载模型）
ENV HF_ENDPOINT=https://hf-mirror.com
# pip 默认走清华镜像（普通包）
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple/
ENV PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn
# conda 包缓存目录（构建完成后清理）
ENV CONDA_PKGS_DIRS=/opt/conda/pkgs

# ── 切换 apt 为阿里云镜像源 ─────────────────────────────────
RUN sed -i 's|http://deb.debian.org|https://mirrors.aliyun.com|g' \
        /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i 's|http://deb.debian.org|https://mirrors.aliyun.com|g' \
        /etc/apt/sources.list 2>/dev/null || true

# ── 安装系统依赖 ────────────────────────────────────────────
# libgl1        : OpenCV headless 仍需要（deepface 内部调用）
# libglib2.0-0  : GLib（OpenCV 依赖）
# libsndfile1   : soundfile / librosa 读写音频
# ffmpeg        : librosa / audioread 解码 mp3 等格式
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
        libsndfile1 \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# ── 配置 conda 清华镜像（仅用于 python 基础包）──────────────
RUN conda config --add channels \
        https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main && \
    conda config --add channels \
        https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/r && \
    conda config --set show_channel_urls yes && \
    conda config --remove channels defaults 2>/dev/null || true

# ── 阶段1：创建 conda 环境（先拷贝环境文件，利用 Docker 缓存）
COPY environment_docker.yml /app/environment_docker.yml
RUN conda env create -f environment_docker.yml && \
    conda clean -afy && \
    find /opt/conda/envs/faceformer_new -name "*.pyc" -delete && \
    find /opt/conda/envs/faceformer_new -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

# 激活 conda 环境
ENV PATH=/opt/conda/envs/faceformer_new/bin:$PATH
ENV CONDA_DEFAULT_ENV=faceformer_new

# ── 阶段2：安装 PyTorch CUDA wheel 并裁剪不必要的大包 ───────
# +cu121 wheel 只有 PyTorch 官方源有，国内镜像均无此版本
# 建议构建时开启代理：docker build --network host ...
RUN pip install --no-cache-dir \
        --timeout 600 \
        --retries 3 \
        torch==2.5.1+cu121 \
        torchaudio==2.5.1+cu121 \
        torchvision==0.20.1+cu121 \
        --index-url https://download.pytorch.org/whl/cu121 \
    || pip install --no-cache-dir \
        --timeout 600 \
        --retries 3 \
        torch==2.5.1+cu121 \
        torchaudio==2.5.1+cu121 \
        torchvision==0.20.1+cu121 \
        --index-url https://mirror.sjtu.edu.cn/pytorch-wheels/cu121 \
    # ── 裁剪推理不需要的大包（在同一层内删除，不产生额外层）──
    && SITE=/opt/conda/envs/faceformer_new/lib/python3.10/site-packages \
    # triton：PyTorch JIT 编译器，推理不需要（555MB）
    && rm -rf $SITE/triton $SITE/triton-*.dist-info \
    # cusparselt：稀疏矩阵库，推理不需要（203MB）
    && rm -rf $SITE/cusparselt $SITE/nvidia_cusparselt* \
    # nvidia/nccl：多机分布式训练库，单机推理不需要（241MB）
    && rm -rf $SITE/nvidia/nccl \
    # nvidia/cuda_nvrtc：运行时 CUDA 编译，推理不需要（61MB）
    && rm -rf $SITE/nvidia/cuda_nvrtc \
    # nvidia/cuda_cupti：性能分析工具，生产不需要（44MB）
    && rm -rf $SITE/nvidia/cuda_cupti \
    # libclang：编译工具，运行时不需要（61MB）
    && pip uninstall -y libclang 2>/dev/null || true \
    # kubernetes：代码未使用（18MB）
    && pip uninstall -y kubernetes 2>/dev/null || true \
    # opencv-python（非 headless）：与 headless 版冲突，删重复库（91MB）
    && pip uninstall -y opencv-python 2>/dev/null || true \
    # sympy：torch 训练依赖，推理不需要（29MB）
    && pip uninstall -y sympy 2>/dev/null || true

# ── 阶段3：预下载 BGE 嵌入模型 ─────────────────────────────
RUN python -c "\
from sentence_transformers import SentenceTransformer; \
SentenceTransformer('BAAI/bge-small-zh-v1.5')"

# ── 阶段4：拷贝模型文件（大文件，放代码之前利用缓存）────────
COPY audio2face_model.pth      /app/audio2face_model.pth
COPY models/                   /app/models/
COPY whisper-final-model/      /app/whisper-final-model/
COPY wav2vec2_xlsr53_local/    /app/wav2vec2_xlsr53_local/
COPY vector_db/                /app/vector_db/

# ── 阶段5：拷贝业务代码和静态资源 ──────────────────────────
COPY *.py    /app/
COPY *.json  /app/
COPY .env    /app/.env
COPY public/ /app/public/

EXPOSE 8000
CMD ["python", "server.py"]
