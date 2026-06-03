### 项目文件夹

还需audio2face_model.pth文件与models/，wav3vec2_xlsr53_local/，whisper-final-model/文件夹，可前往网盘下载：
通过网盘分享的文件：demo
链接: https://pan.baidu.com/s/1qwV5kME_K_yN5VlvVAiaOQ?pwd=4d22 提取码: 4d22
--来自百度网盘超级会员v1的分享

将网盘中demo/文件夹所有文件放到项目对应位置即可。

---

### 环境安装

```bash
cd demo
pip install -r requirements.txt
```

### 下载模型（含共情反应所需的 SER 模型）

```bash
cd demo
python download_models.py     # 下载 whisper-small / SER 语音情感 / BGE 知识库 三个模型到 ./models/
python download_xlsr53.py     # 下载 wav2vec2-xlsr53（口型/音频特征）
```

- **SER 语音情感模型**是「共情反应」功能所需：`download_models.py` 会把 `xmj2002/hubert-base-ch-speech-emotion-recognition` 下到 `./models/ser-model/`，正好对应代码默认路径，无需额外配置。
- 不下载也不会让程序崩溃，但**共情表情会静默失效**（SER 加载失败时自动跳过）。
- 想换别的 SER 模型，用环境变量 `SER_MODEL` 指定本地目录或 HuggingFace 模型名（见下方「环境变量」）。
- 以上模型也可从网盘 `models/` 直接下载（见顶部链接），二选一即可。

主要依赖（已在 `requirements.txt` 列出）：
- **FastAPI / uvicorn** — Web 服务和 WebSocket
- **aiohttp** — LLM 流式请求
- **edge-tts / azure-cognitiveservices-speech** — TTS 与 viseme
- **whisper / faster-whisper** — ASR
- **deepface / opencv-python** — 摄像头表情识别
- **transformers / torch** — 语音情感识别 SER（共情反应）
- **chromadb / sentence-transformers** — 心理学知识库 RAG 检索
- **mcp** — MCP 客户端 SDK（连远程天气服务）
- **zhdate**（可选）— 农历转换（未装则跳过农历显示）

### 环境变量（.env）

```ini
API_KEY=<SiliconFlow / 兼容 OpenAI 接口的 API Key>
AZURE_SPEECH_KEY=<Azure 语音服务 Key>
AZURE_SPEECH_REGION=<区域，如 southeastasia>

# 可选覆盖（默认值已写在 tools.py）
# WEATHER_MCP_URL=https://mcp.api-inference.modelscope.net/fd58fbe7964240/sse
# DEFAULT_CITY=北京

# ---- 语音情感识别 SER（共情反应，均为可选，代码已内置默认值）----
# SER 模型：默认用本地 models/ser-model；也可填 HuggingFace 模型名（首次自动下载）
# SER_MODEL=firdhokk/speech-emotion-recognition-with-facebook-wav2vec2-large-xlsr-53
# 去偏置阈值：主情感为中性时，非中性情感≥该值就优先采用（0~1，越小越敏感）
# SER_NON_NEUTRAL_THRESHOLD=0.20
```

### 启动

```bash
cd demo
python server.py
# 浏览器访问 http://localhost:8000/
```

### 架构要点

- **前端**：`public/index.html` + `public/script.js`，Tailwind + Lucide + DiceBear + TalkingHead 3D
- **LLM**：SiliconFlow Qwen2.5-7B-Instruct，流式输出，按句分片送 TTS
- **工具**：仅天气走远程 MCP（实时数据），其它快捷按钮由 LLM 自身知识回答
- **关怀模式**：`html.care-mode` 抬高根字号 25%，整页 rem 单位等比放大
- **共情反应**：用户语音经 SER 识别情感（`audio_emotion` → `faceformer_adapter.audio_bytes_to_user_emotion`），后端回推 `user_emotion`，数字人在 LLM 思考间隙切换为对应共情表情（陪伴式：不镜像负面情绪，转为关切）。SER 对 neutral 做去偏置，可调阈值见上方环境变量
