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

主要依赖（已在 `requirements.txt` 列出）：
- **FastAPI / uvicorn** — Web 服务和 WebSocket
- **aiohttp** — LLM 流式请求
- **edge-tts / azure-cognitiveservices-speech** — TTS 与 viseme
- **whisper / faster-whisper** — ASR
- **deepface / opencv-python** — 摄像头表情识别
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
