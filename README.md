# Digtal-Human-Companion-System

【A22】基于AI大语言模型的情感陪护虚拟数字人系统

---

## 项目介绍

面向老年群体的情感陪护数字人：实时语音对话（ASR/TTS）、表情驱动、心理危机预警、提醒、天气/新闻等工具调用，并基于本地知识库（PsyQA + SoulChat）做 RAG 心理干预。

---

## 仓库内已包含 / 需另外获取

随仓库同步（clone 即得，**私有仓库**，含 `.env` 密钥，无需再配）：

- 全部源码、前端、3D 头部模型（`demo/public/*.glb`）
- Whisper LoRA 微调权重（`demo/whisper-final-model/`）
- 危机风险分类器 `demo/models/crisis-bert/classifier.pkl`（SOS-1K 训练，开箱即用）
- `demo/.env`（已含 DeepSeek、Azure 等密钥）

**体积过大（>100MB，GitHub 无法同步）需另外下载**，见下方步骤：

| 文件 | 大小 | 用途 | 获取方式 |
| --- | --- | --- | --- |
| `demo/models/`（whisper-small / ser-model / bge） | ~0.9GB | ASR / 语音情感 / 向量模型 | 运行 `download_models.py` 自动下载 |
| `demo/psy_data.json` | 107MB | 建库语料（PsyQA） | 内部群已分享，放到 `demo/` 下即可 |
| `demo/vector_db/` | ~1.7GB | 向量数据库 | 运行 `build_kb.py` 在线重建（无需网盘） |
| `demo/datasets/SoulChat/` | 348MB | SoulChat 多轮语料（完整功能必需） | HuggingFace 在线下载（见步骤 5） |
| `demo/datasets/SOS-1K/`（仅重训用） | 10MB | 危机分类重训语料 | GitHub 克隆（见步骤 6） |

> 完整功能需获取上表「仅重训用」之外的全部项。`vector_db/` 与 `datasets/SoulChat/` 都由脚本在线生成/下载，无需走网盘。
> `audio2face_model.pth`、`wav2vec2_xlsr53_local/` 属旧版口型方案的遗留文件，当前服务由 SER 模型驱动表情，**无需下载**。

---

## 快速部署

> **完整功能命令速览**（详细说明见下方各步骤，均在 `demo/` 目录下执行）：
>
> ```bash
> conda env create -f environment.yml --name dh && conda activate dh   # 1 环境
> python download_models.py                                            # 2 下模型(whisper/ser/bge)
> python build_kb.py                                                   # 3 建 PsyQA 库(需先放好 psy_data.json)
> python -c "from huggingface_hub import snapshot_download; snapshot_download('Spiderman01/soulchat_split_raw', repo_type='dataset', allow_patterns='data/*.parquet', local_dir='datasets/SoulChat')"
> python build_kb_soulchat.py                                          # 4 建 SoulChat 库(完整功能必做)
> python server.py                                                     # 5 启动 → http://localhost:8000
> ```
>
> 危机分类器(`classifier.pkl`)、ASR 微调权重、`.env` 密钥已随仓库同步，无需额外操作。

### 1. 克隆仓库

```bash
git clone https://github.com/wt-5783/Digtal-Human-Companion-System.git
cd Digtal-Human-Companion-System/demo
```

### 2. 安装环境

conda（推荐，依赖版本已锁定）：

```bash
conda env create -f environment.yml --name 你的环境名
conda activate 你的环境名
```

或用 pip / uv：`pip install -r requirements.txt`

### 3. 下载模型文件

运行下载脚本，自动从 HuggingFace（默认走国内镜像 hf-mirror.com）拉取**运行所需的三个模型**到 `demo/models/`：

```bash
python download_models.py     # whisper-small（ASR）/ ser-model（语音情感）/ bge（向量检索）
```

> `whisper-final-model/`（ASR 微调权重）和危机分类器 `models/crisis-bert/classifier.pkl` 已随仓库同步，无需下载。
> 摄像头表情用的 DeepFace 模型会在首次使用时自动联网下载。
> `audio2face_model.pth`、`wav2vec2_xlsr53_local/` 为旧版遗留，跑服务用不到；如需复现旧口型脚本再从网盘获取：
>
> ```plaintext
> 网盘（项目大文件）：https://pan.baidu.com/s/1lni_D5WtU01ISW8zi4oQWQ?pwd=66mg 提取码: 66mg
> ```

### 4. 准备语料并建立知识库

把 `psy_data.json`（内部群已分享，原始数据来自 [thu-coai/PsyQA](https://github.com/thu-coai/PsyQA)）放到 `demo/` 下，然后一条命令在线建库（首次会自动下载 `BGE-small-zh-v1.5`，几分钟跑完）：

```bash
python build_kb.py
```

### 5. 建立 SoulChat 知识库（完整功能必做）

RAG 的多轮安抚话术依赖此库，跑全部功能必须执行本步（跳过则只用 PsyQA，多轮共情会变弱）。语料来自 [SoulChat](https://www.modelscope.cn/datasets/YIRONGCHEN/SoulChatCorpus)（华南理工），下方用其 parquet 镜像，**下载后目录结构须与脚本一致**：

```bash
# 在 demo/ 目录下执行；需要 huggingface_hub（requirements 已含）
# Windows 走国内镜像更快：set HF_ENDPOINT=https://hf-mirror.com
python -c "from huggingface_hub import snapshot_download; snapshot_download('Spiderman01/soulchat_split_raw', repo_type='dataset', allow_patterns='data/*.parquet', local_dir='datasets/SoulChat')"
```

下载后应得到 `demo/datasets/SoulChat/data/*.parquet`（约 350MB，含 `id/topic/messages` 列），再建库：

```bash
python build_kb_soulchat.py    # 取每段对话首轮，采样 8 万条写入 soulchat_knowledge 集合
```

> 容错：若未建此库，`llm.py` 检测不到 SoulChat 集合会自动跳过、不报错，但属功能不完整状态。

### 6. 危机风险分类器（已随仓库安装，无需操作）

危机预警用的 SOS-1K 分类器 `models/crisis-bert/classifier.pkl` 已随仓库同步，**clone 后即生效，无需任何安装步骤**。

仅当你想用 SOS-1K 语料**重新训练**它时（开发者场景），才需克隆数据集后运行：

```bash
# 在 demo/ 目录下执行；目录名必须为 datasets/SOS-1K
git clone https://github.com/HongzhiQ/FineGrainedSuicideDetection.git datasets/SOS-1K
python train_crisis_classifier.py    # 输出覆盖 models/crisis-bert/classifier.pkl
```

### 7. 配置 `.env`

`.env` 已随仓库同步，含 DeepSeek（`API_KEY`，模型 `deepseek-v4-pro`）与 Azure 语音密钥，正常情况无需改动。如需更换，编辑 `demo/.env`：

```ini
API_KEY=<DeepSeek API Key>              # LLM，接口 https://api.deepseek.com
AZURE_SPEECH_KEY=<Azure 语音密钥>        # TTS / ASR
AZURE_SPEECH_REGION=<区域，如 eastasia>
# NEWS_MCP_URL / WEATHER_MCP_URL / DEFAULT_CITY 可选
```

### 8. 启动项目

```bash
python server.py
```

浏览器打开 `http://localhost:8000`。

---

## 镜像打包

### 1. 启动 Docker（Windows 为例）

双击运行 Docker Desktop。

### 2. 打包镜像

```bash
cd 项目目录
docker compose build
```

### 也提供打包好的镜像下载：

```plaintext
通过网盘分享的文件：digital-human.tar.gz
链接: https://pan.baidu.com/s/1O3mpjuZEwIx1IbqzgB5stg?pwd=eyqz 提取码: eyqz
```
