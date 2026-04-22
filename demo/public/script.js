// ============================================================
// script.js - 重构版：模块化组织，集中状态管理
// ============================================================

// ---------------------- CONFIG ----------------------
const AVATARS = [
  null,
  { id: 1, name: '小丽', desc: '温柔可爱，陪你聊天～', icon: '👧', skinClass: 'avatar-friend1', welcome: '你好呀～我是小丽，很高兴认识你！', modelPath: '/static/3d卡通少女头部模型.glb' },
  { id: 2, name: '老王', desc: '风趣幽默，随时唠嗑～', icon: '👴', skinClass: 'avatar-friend2', welcome: '你好，我是老王，咱们随便聊！', modelPath: '/static/3d卡通老人头部模型.glb' },
  { id: 3, name: '小明', desc: '年轻伙伴，活力陪聊～', icon: '🧑', skinClass: 'avatar-friend3', welcome: '你好，我是小明，和你聊聊生活、兴趣、好心情！', modelPath: '/static/3d卡通少年头部模型.glb' },
];

const STATUS = {
  online: { key: 'online', text: '👋 在线' },
  thinking: { key: 'thinking', text: '🤔 思考中...' },
  speaking: { key: 'speaking', text: '💬 正在说话' },
  playAudio: { key: 'speaking', text: '🔊 播放语音' },
  listening: { key: 'listening', text: '🎤 正在听您说话...' },
  listenSay: { key: 'listening', text: '🎤 我在听，您说' },
  offline: { key: 'offline', text: '⚠️ 离线' },
  offlineD: { key: 'offline', text: '⚠️ 连接断开' },
  reconnect: { key: 'reconnecting', text: '🔄 重连中...' },
  genReply: { key: 'thinking', text: '⚡ 正在生成回复...' },
};

const MAX_RECONNECT = 5;

// ---------------------- state ----------------------
const state = {
  avatar: null, sessionId: null, sessions: [], userName: '',
  ws: null, wsConnected: false, reconnects: 0,
  recording: false, recognition: null,
  responseId: null, botChunks: [], // 每个 chunk 的文本数组（当前轮次）
  currentChunkEl: null, // 当前正在填充的气泡元素
  thinkingEl: null, pressTimer: null, isLongPress: false,
  bargeInActive: false, // 标记 barge_in_start 已发送，防止 sendMessage 重复 reset
};

// ---------------------- dom ----------------------
const dom = {};
function cacheDom() {
  ['selectPage', 'chatPage', 'botName', 'botDesc', 'messageInput',
    'messagesContainer', 'avatarContainer', 'sessionList', 'sendBtn',
    'timeGreeting', 'userDisplayName'].forEach(id => {
      dom[id] = document.getElementById(id);
    });
}

// ---------------------- utils ----------------------
function scrollToBottom() {
  const area = document.querySelector('.chat-history-area');
  if (area) setTimeout(() => { area.scrollTop = area.scrollHeight; }, 10);
}

function showToast(msg, type = 'info') {
  const t = document.createElement('div');
  t.className = `toast-message ${type}`;
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => { t.style.animation = 'fadeOutDown 0.3s ease'; setTimeout(() => t.remove(), 300); }, 3000);
}

function base64ToBlob(b64, mime) {
  const raw = atob(b64), arr = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i++) arr[i] = raw.charCodeAt(i);
  return new Blob([arr], { type: mime });
}

function setStatus(s) {
  const el = document.getElementById('statusText');
  if (el) el.textContent = s.text;
  const light = document.getElementById('breathingLight');
  if (!light) return;
  light.classList.remove('active', 'listening', 'thinking', 'speaking');
  if (['listening', 'thinking', 'speaking'].includes(s.key)) light.classList.add('active', s.key);
}

function getGreeting() {
  const h = new Date().getHours();
  if (h >= 5 && h < 9) return '🌅 早上好';
  if (h >= 9 && h < 12) return '☀️ 上午好';
  if (h >= 12 && h < 14) return '🌞 中午好';
  if (h >= 14 && h < 18) return '🌤️ 下午好';
  if (h >= 18 && h < 22) return '🌙 晚上好';
  return '🌛 夜深了';
}

function getTimePrefix() {
  const h = new Date().getHours();
  if (h >= 5 && h < 12) return '早上好';
  if (h >= 12 && h < 14) return '中午好';
  if (h >= 14 && h < 18) return '下午好';
  return '晚上好';
}

function personalGreeting(base) {
  const name = state.userName ? `，${state.userName}` : '';
  return `${getTimePrefix()}${name}！${base}`;
}

function splitSentences(text) {
  const s = (text || '').replace(/\s+/g, ' ').trim();
  if (!s) return [];
  return (s.match(/[^。！？!?；;]+[。！？!?；;]?/g) || [s]).map(i => i.trim()).filter(Boolean);
}

// ---------------------- AudioPlayer ----------------------
const AudioPlayer = {
  el: new Audio(),
  queue: [],          // 每项: { url, responseId, seq, visemes }
  playing: false,
  currentUrl: null,
  responseId: null,
  playStartTime: null,  // 当前音频实际开始播放的 Date.now() 时间戳

  enqueue(base64Audio, responseId, seq = 0, visemes = []) {
    try {
      const blob = base64ToBlob(base64Audio, 'audio/mpeg');
      const url = URL.createObjectURL(blob);
      this.queue.push({ url, responseId, seq: Number(seq) || 0, visemes });
      this.queue.sort((a, b) => a.seq - b.seq);
      this.playNext();
    } catch (e) { showToast('音频入队失败', 'warning'); }
  },

  playNext() {
    if (this.playing) return;
    const next = this.queue.shift();
    if (!next) { this.responseId = null; setStatus(STATUS.online); return; }
    this.playing = true;
    this.responseId = next.responseId;
    this.currentUrl = next.url;
    this.el.pause();
    this.el.currentTime = 0;
    this.el.src = next.url;
    this.el.onended = () => this.finish();
    this.el.onerror = () => this.finish();
    this.el.play()
      .then(() => {
        this.playStartTime = Date.now();
        if (next.visemes && next.visemes.length > 0) {
          scheduleVisemeAnimation(next.visemes, this.el);
        }
      })
      .catch(() => { showToast('浏览器阻止了音频自动播放', 'warning'); this.finish(); });
  },

  finish() {
    const url = this.currentUrl;
    this.el.onended = null;
    this.el.onerror = null;
    this.el.removeAttribute('src');
    this.el.load();
    if (url) URL.revokeObjectURL(url);
    this.currentUrl = null;
    this.playing = false;
    this.playStartTime = null;
    setTimeout(() => this.playNext(), 40);
  },

  reset() {
    this.el.pause();
    this.el.currentTime = 0;
    this.el.removeAttribute('src');
    this.el.load();
    this.el.onended = null;
    this.el.onerror = null;
    this.queue.forEach(item => { if (item.url) URL.revokeObjectURL(item.url); });
    this.queue = [];
    this.playing = false;
    this.responseId = null;
    this.playStartTime = null;
    if (this.currentUrl) { URL.revokeObjectURL(this.currentUrl); this.currentUrl = null; }
    clearVisemeTimers();   // 清除所有挂起的口型定时器
    setAvatarMouth(0);     // 重置嘴型为静默状态
  },
};

// ---------------------- Bot Message Rendering ----------------------
// 每个 chunk 作为独立气泡显示

/** 为当前 chunk 创建一个新的独立气泡 */
function createBotBubble(responseId, text) {
  if (state.responseId !== responseId) {
    state.responseId = responseId;
    state.botChunks = [];
    state.currentChunkEl = null;
  }
  const div = document.createElement('div');
  div.className = 'message message-bot';
  div.dataset.responseId = String(responseId);
  div.textContent = text;
  dom.messagesContainer.appendChild(div);
  state.currentChunkEl = div;
  state.botChunks.push(text);
  scrollToBottom();
  return div;
}

/** 更新当前气泡的文本（用于 text_delta 流式追加到当前 chunk） */
function updateCurrentBubble(responseId, text) {
  if (state.responseId !== responseId) {
    state.responseId = responseId;
    state.botChunks = [];
    state.currentChunkEl = null;
  }
  if (!state.currentChunkEl) {
    return createBotBubble(responseId, text);
  }
  // 更新最后一个气泡的文本
  state.currentChunkEl.textContent = text;
  state.botChunks[state.botChunks.length - 1] = text;
  scrollToBottom();
}

/** 获取当前轮次所有气泡的合并文本 */
function getBotFullText() {
  return state.botChunks.join('');
}

/** 重置当前轮次的气泡状态（不删除已显示的 DOM） */
function resetBotBubbleState() {
  state.botChunks = [];
  state.currentChunkEl = null;
}

function showThinking(show) {
  if (show) {
    if (!state.thinkingEl) {
      state.thinkingEl = document.createElement('div');
      state.thinkingEl.className = 'message message-thinking';
      state.thinkingEl.innerHTML = '<span class="thinking-dots">🤔 正在思考...</span>';
      dom.messagesContainer.appendChild(state.thinkingEl);
      scrollToBottom();
    }
  } else if (state.thinkingEl) {
    state.thinkingEl.remove();
    state.thinkingEl = null;
  }
}

// ---------------------- WebSocket ----------------------
function wsSend(data) {
  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    state.ws.send(JSON.stringify(data));
  }
}

function handleChunk(d) {
  showThinking(false);
  const rid = d.responseId || state.responseId || `${Date.now()}`;
  const chunkText = (d.text || '').trim();
  // 只在文本尚未通过 text_delta 显示时才创建气泡（fallback 场景）
  if (chunkText && !state.botChunks.includes(chunkText)) {
    createBotBubble(rid, chunkText);
  }
  const audio = d.data || d.audio;
  if (audio) {
    setStatus(STATUS.playAudio);
    AudioPlayer.enqueue(audio, rid, d.seq, d.visemes || []);
  }
}

const msgHandlers = {
  text(d) {
    const text = d.content || d.text || '';
    if (!text) return;
    showThinking(false);
    setStatus(STATUS.speaking);
    addHistory('bot', text);
    addMsgToSession('bot', text);
    setTimeout(() => setStatus(STATUS.online), 1000);
  },
  audio(d) {
    const audio = d.data || d.audio;
    if (!audio) return;
    showThinking(false);
    setStatus(STATUS.playAudio);
    AudioPlayer.enqueue(audio, state.responseId || `${Date.now()}`, 0);
    setTimeout(() => setStatus(STATUS.online), 2000);
  },
  audio_chunk: handleChunk,
  assistant_chunk: handleChunk,
  assistant_text_delta(d) {
    showThinking(false);
    const rid = d.responseId || state.responseId || `${Date.now()}`;
    // 每个 delta 是一个完整的 chunk（一句话），创建独立气泡
    const delta = d.delta || d.text || '';
    if (delta) {
      createBotBubble(rid, delta);
    }
  },
  // 用户说话结束后，服务器推送数字人的聆听反应序列（audio2face_model 正确用途）
  listener_reaction(d) {
    const frames = d.frames || [];
    const fps = d.fps || 25;
    if (!frames.length) return;
    console.log(`[listener_reaction] 收到 ${frames.length} 帧聆听反应，fps=${fps}`);
    playListenerReactionAnimation(frames, fps);
  },
  turn_start(d) {
    showThinking(false);
    // 数字人开始说话，停止聆听反应动画
    if (listenerAnimTimer !== null) { clearInterval(listenerAnimTimer); listenerAnimTimer = null; }
    clearVisemeTimers();  // 清除上一轮残留口型定时器
    setAvatarMouth(0);    // 重置嘴型
    AudioPlayer.reset();
    // 重置气泡状态，准备新一轮
    resetBotBubbleState();
    state.responseId = d.responseId || `${Date.now()}`;
    setStatus(STATUS.genReply);
  },
  stop_output(d) {
    clearVisemeTimers();
    setAvatarMouth(0);
    AudioPlayer.reset();
    showThinking(false);
    // 保存已显示的文本到 session
    const fullText = getBotFullText();
    if (fullText) addMsgToSession('bot', fullText + '…');
    resetBotBubbleState();
    state.responseId = null;
    setStatus(STATUS.listenSay);
  },
  turn_interrupted(d) {
    clearVisemeTimers();
    setAvatarMouth(0);
    AudioPlayer.reset();
    showThinking(false);
    // 保存被打断时已说出的文本
    const spokenText = d.spokenText || getBotFullText();
    if (spokenText) addMsgToSession('bot', spokenText + '…');
    resetBotBubbleState();
    state.responseId = null;
    setStatus(STATUS.listenSay);
  },
  listen_state() { setStatus(STATUS.listenSay); },
  turn_end(d) {
    showThinking(false);
    // 合并所有气泡文本存入 session
    const fullText = d.fullText || getBotFullText();
    if (fullText) addMsgToSession('bot', fullText);
    resetBotBubbleState();
    state.responseId = null;
    if (!AudioPlayer.playing && AudioPlayer.queue.length === 0) setStatus(STATUS.online);
  },
  thinking(d) {
    showThinking(!!d.status);
    setStatus(d.status ? STATUS.thinking : STATUS.online);
  },
  error(d) { showToast(d.message || '服务器处理失败', 'error'); },
};

function handleServerMessage(data) {
  if (!data || !data.type) return;
  const handler = msgHandlers[data.type];
  if (handler) handler(data);
}

function connectWebSocket() {
  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  state.ws = new WebSocket(`${protocol}//${location.host}/ws/chat`);

  state.ws.onopen = () => {
    state.wsConnected = true;
    state.reconnects = 0;
    setStatus(STATUS.online);
    if (state.avatar) {
      wsSend({ type: 'init', avatarId: state.avatar.id, avatarName: state.avatar.name, sessionId: state.sessionId, userName: state.userName });
    }
  };
  state.ws.onmessage = (e) => {
    try { handleServerMessage(JSON.parse(e.data)); } catch (_) { showToast('服务器消息解析失败', 'warning'); }
  };
  state.ws.onerror = () => { state.wsConnected = false; setStatus(STATUS.offlineD); };
  state.ws.onclose = () => {
    state.wsConnected = false;
    setStatus(STATUS.offline);
    if (state.reconnects < MAX_RECONNECT) {
      state.reconnects++;
      setStatus(STATUS.reconnect);
      setTimeout(connectWebSocket, Math.min(1000 * Math.pow(2, state.reconnects), 10000));
    } else {
      showToast('无法连接到服务器，请刷新页面重试', 'error');
    }
  };
}

// ---------------------- Lip-sync Viseme Animation ----------------------
// Azure TTS viseme_id (0~21) + time_ms 时间轴
// 用 requestAnimationFrame + audio.currentTime 做实时对齐，支持自适应 morphTarget 映射

/**
 * Microsoft Viseme ID (0~21) → ARKit blendshape 组合权重（标准 ARKit 模型用）。
 * 每个 viseme 对应多个 morphTarget 的目标权重（0~1）。
 * 参考微软官方 viseme-to-phoneme 映射 + ARKit 面部动作标准。
 * 权重值经过调大以确保视觉上能明显看出口型变化。
 */
const VISEME_TO_ARKIT = {
  // 0: 静默 - 嘴自然闭合
  0: {},
  // 1: æ, ə, ʌ - 微张嘴，舌头放松
  1: { jawOpen: 0.45, mouthLowerDownLeft: 0.2, mouthLowerDownRight: 0.2 },
  // 2: ɑ - 大张嘴
  2: { jawOpen: 0.85, mouthLowerDownLeft: 0.45, mouthLowerDownRight: 0.45, mouthStretchLeft: 0.15, mouthStretchRight: 0.15 },
  // 3: ɔ - 圆唇半开
  3: { jawOpen: 0.55, mouthFunnel: 0.65, mouthPucker: 0.2 },
  // 4: e, ɛ - 扁唇微张，嘴角略拉
  4: { jawOpen: 0.30, mouthSmileLeft: 0.50, mouthSmileRight: 0.50, mouthStretchLeft: 0.25, mouthStretchRight: 0.25 },
  // 5: ɪ - 扁唇窄开
  5: { jawOpen: 0.15, mouthSmileLeft: 0.55, mouthSmileRight: 0.55, mouthStretchLeft: 0.3, mouthStretchRight: 0.3 },
  // 6: w, ʊ - 圆唇突出
  6: { jawOpen: 0.20, mouthPucker: 0.70, mouthFunnel: 0.50 },
  // 7: u - 圆唇紧缩
  7: { jawOpen: 0.10, mouthPucker: 0.75, mouthFunnel: 0.55, mouthShrugUpper: 0.2 },
  // 8: o - 圆唇大开
  8: { jawOpen: 0.60, mouthFunnel: 0.70, mouthPucker: 0.3 },
  // 9: aʊ - 从大开到圆唇（取中间态）
  9: { jawOpen: 0.50, mouthFunnel: 0.45, mouthPucker: 0.35, mouthLowerDownLeft: 0.2, mouthLowerDownRight: 0.2 },
  // 10: ɔɪ - 圆唇到扁唇过渡
  10: { jawOpen: 0.55, mouthFunnel: 0.35, mouthSmileLeft: 0.25, mouthSmileRight: 0.25 },
  // 11: aɪ - 大开到扁唇
  11: { jawOpen: 0.65, mouthSmileLeft: 0.30, mouthSmileRight: 0.30, mouthLowerDownLeft: 0.3, mouthLowerDownRight: 0.3 },
  // 12: h - 微张嘴送气
  12: { jawOpen: 0.35, mouthLowerDownLeft: 0.15, mouthLowerDownRight: 0.15 },
  // 13: ɹ - 嘴微圆，舌卷
  13: { jawOpen: 0.25, mouthFunnel: 0.40, mouthRollLower: 0.30, mouthShrugLower: 0.2 },
  // 14: l - 舌尖抵上齿龈，嘴微开
  14: { jawOpen: 0.35, mouthLowerDownLeft: 0.25, mouthLowerDownRight: 0.25, mouthPressLeft: 0.15, mouthPressRight: 0.15 },
  // 15: s, z - 齿缝音，嘴几乎闭合但有缝
  15: { jawOpen: 0.08, mouthSmileLeft: 0.35, mouthSmileRight: 0.35, mouthClose: 0.3, mouthStretchLeft: 0.2, mouthStretchRight: 0.2 },
  // 16: ʃ, tʃ - 嘴唇前突微圆
  16: { jawOpen: 0.15, mouthFunnel: 0.55, mouthPucker: 0.40, mouthShrugUpper: 0.15 },
  // 17: ð, θ - 舌尖在齿间
  17: { jawOpen: 0.12, mouthLowerDownLeft: 0.20, mouthLowerDownRight: 0.20, mouthRollLower: 0.15, mouthClose: 0.2 },
  // 18: f, v - 上齿咬下唇
  18: { jawOpen: 0.08, mouthRollLower: 0.50, mouthUpperUpLeft: 0.25, mouthUpperUpRight: 0.25, mouthShrugUpper: 0.2 },
  // 19: d, t, n - 舌尖抵上齿龈，嘴微闭
  19: { jawOpen: 0.18, mouthClose: 0.35, mouthPressLeft: 0.20, mouthPressRight: 0.20 },
  // 20: k, g - 舌根音，嘴微开
  20: { jawOpen: 0.30, mouthLowerDownLeft: 0.20, mouthLowerDownRight: 0.20, mouthStretchLeft: 0.1, mouthStretchRight: 0.1 },
  // 21: p, b, m - 双唇闭合
  21: { jawOpen: 0.03, mouthClose: 0.60, mouthPressLeft: 0.40, mouthPressRight: 0.40, mouthPucker: 0.15 },
};

/** viseme_id → 开口幅度（0~1），用于 fallback 简化驱动 */
const VISEME_OPEN_AMOUNT = [
  0,    // 0  静默
  0.35, // 1  ə
  0.80, // 2  ɑ 大开
  0.50, // 3  ɔ
  0.25, // 4  eɪ
  0.15, // 5  ɪ
  0.20, // 6  ʊ
  0.10, // 7  iː
  0.70, // 8  aʊ
  0.20, // 9  oʊ
  0.70, // 10 aɪ
  0.40, // 11 ɔɪ
  0.20, // 12 eɪ
  0.30, // 13 ɝ
  0.80, // 14 aː
  0.70, // 15 a
  0.25, // 16 e
  0.10, // 17 i
  0.15, // 18 w
  0.10, // 19 s
  0.20, // 20 θ
  0.25, // 21 uː
];

/** 所有需要在切换 viseme 时归零的口部 ARKit blendshape 名称 */
const MOUTH_ARKIT_NAMES = [
  'jawOpen', 'jawForward', 'jawLeft', 'jawRight',
  'mouthClose', 'mouthFunnel', 'mouthPucker',
  'mouthLeft', 'mouthRight',
  'mouthSmileLeft', 'mouthSmileRight',
  'mouthFrownLeft', 'mouthFrownRight',
  'mouthDimpleLeft', 'mouthDimpleRight',
  'mouthStretchLeft', 'mouthStretchRight',
  'mouthRollLower', 'mouthRollUpper',
  'mouthShrugLower', 'mouthShrugUpper',
  'mouthPressLeft', 'mouthPressRight',
  'mouthLowerDownLeft', 'mouthLowerDownRight',
  'mouthUpperUpLeft', 'mouthUpperUpRight',
];

// ── 自适应映射 ────────────────────────────────────────────────────────────────
/**
 * 模型加载后调用，检测实际 morphTarget 名称，建立自适应驱动策略。
 * 结果存入 _morphMode / _fallbackMorphName / _fallbackMorphIdx。
 *
 * 三种模式：
 *   'arkit'    - 模型含标准 ARKit blendshape（jawOpen 等），直接用 VISEME_TO_ARKIT
 *   'fallback' - 找到一个口部相关 morph，用 VISEME_OPEN_AMOUNT 驱动开合幅度
 *   'none'     - 模型无任何 morphTarget，跳过
 */
let _morphMode = 'none';
let _fallbackMorphName = null;
let _fallbackMorphIdx = -1;
let _fallbackMeshes = []; // [{mesh, idx}]

function buildAdaptiveVisemeMap() {
  _morphMode = 'none';
  _fallbackMorphName = null;
  _fallbackMorphIdx = -1;
  _fallbackMeshes = [];

  if (!avatarModel) return;

  // 收集所有 morphTarget 名称
  const allNames = new Set();
  avatarModel.traverse(child => {
    if (child.isMesh && child.morphTargetDictionary) {
      Object.keys(child.morphTargetDictionary).forEach(n => allNames.add(n));
    }
  });

  console.log('[Viseme] 模型 morphTarget 列表:', [...allNames]);

  // 检查是否有标准 ARKit blendshape
  if (allNames.has('jawOpen') || allNames.has('mouthOpen')) {
    _morphMode = 'arkit';
    console.log('[Viseme] 模式: arkit（标准 ARKit blendshape）');
    return;
  }

  // 尝试找口部相关 morph（不区分大小写）
  const mouthKeywords = ['mouth', 'jaw', 'open', 'lip', 'speak', 'talk', 'viseme'];
  let found = null;
  for (const name of allNames) {
    const lower = name.toLowerCase();
    if (mouthKeywords.some(k => lower.includes(k))) { found = name; break; }
  }
  // 没找到关键词就用第一个 morph
  if (!found && allNames.size > 0) found = [...allNames][0];

  if (found) {
    _morphMode = 'fallback';
    _fallbackMorphName = found;
    console.log(`[Viseme] 模式: fallback，使用 morphTarget="${found}" 驱动开合幅度`);

    // 预先收集含该 morph 的所有 mesh
    avatarModel.traverse(child => {
      if (child.isMesh && child.morphTargetDictionary && found in child.morphTargetDictionary) {
        _fallbackMeshes.push({ mesh: child, idx: child.morphTargetDictionary[found] });
      }
    });
  } else {
    console.warn('[Viseme] 模式: none，模型无任何 morphTarget，口型动画不可用');
  }
}

// ── rAF 驱动的实时口型动画 ────────────────────────────────────────────────────
let _rafId = null;
let _visemeData = null;   // { visemes: [{time_ms, viseme_id}], audioEl: HTMLAudioElement }
let _currentWeights = {}; // 当前各 morphTarget 的实际权重（用于 lerp）
const LERP_SPEED = 12;    // 每秒插值速度（降低以获得更平滑自然的口型过渡）
let _lastLoggedViseme = -1; // 调试用：上次打印的 viseme id

/** 停止 rAF 口型动画循环 */
function clearVisemeTimers() {
  if (_rafId !== null) { cancelAnimationFrame(_rafId); _rafId = null; }
  _visemeData = null;
}

/**
 * 启动 rAF 口型动画。
 * @param {Array<{time_ms: number, viseme_id: number}>} visemes
 * @param {HTMLAudioElement} audioEl - 正在播放的 Audio 元素
 */
function scheduleVisemeAnimation(visemes, audioEl) {
  clearVisemeTimers();
  if (!visemes || visemes.length === 0) {
    console.warn('[Viseme] 收到空 viseme 数据，跳过口型动画');
    return;
  }
  console.log(`[Viseme] 启动口型动画: ${visemes.length} 个 viseme 帧, 模式=${_morphMode}`);
  _visemeData = { visemes, audioEl };
  _currentWeights = {};
  _lastLoggedViseme = -1;
  let lastTs = null;

  function tick(ts) {
    if (!_visemeData) return;
    const dt = lastTs !== null ? Math.min((ts - lastTs) / 1000, 0.1) : 0;
    lastTs = ts;

    const audioEl = _visemeData.audioEl;
    const currentMs = audioEl.paused ? null : audioEl.currentTime * 1000;

    // 找当前时刻对应的 viseme（取最后一个 time_ms <= currentMs 的）
    let visemeId = 0;
    if (currentMs !== null) {
      for (let i = _visemeData.visemes.length - 1; i >= 0; i--) {
        if (_visemeData.visemes[i].time_ms <= currentMs) {
          visemeId = _visemeData.visemes[i].viseme_id;
          break;
        }
      }
    }

    // 调试日志：viseme 切换时打印
    if (visemeId !== _lastLoggedViseme) {
      _lastLoggedViseme = visemeId;
      // 仅在开发时启用详细日志，避免刷屏
      // console.log(`[Viseme] id=${visemeId}, time=${currentMs?.toFixed(0)}ms`);
    }

    // 计算目标权重
    const targetWeights = _buildTargetWeights(visemeId);

    // lerp 当前权重 → 目标权重
    const alpha = Math.min(1, LERP_SPEED * dt);
    const allKeys = new Set([...Object.keys(_currentWeights), ...Object.keys(targetWeights)]);
    allKeys.forEach(k => {
      const cur = _currentWeights[k] ?? 0;
      const tgt = targetWeights[k] ?? 0;
      _currentWeights[k] = cur + (tgt - cur) * alpha;
    });

    _applyWeights(_currentWeights);

    // 音频结束后再跑 0.3s 让嘴自然闭合
    if (audioEl.paused || audioEl.ended) {
      const allZero = Object.values(_currentWeights).every(v => v < 0.01);
      if (allZero) { clearVisemeTimers(); return; }
    }

    _rafId = requestAnimationFrame(tick);
  }

  _rafId = requestAnimationFrame(tick);
}

/** 根据 visemeId 和当前模式计算目标权重 map */
function _buildTargetWeights(visemeId) {
  if (_morphMode === 'arkit') {
    return VISEME_TO_ARKIT[visemeId] ?? {};
  }
  if (_morphMode === 'fallback' && _fallbackMorphName) {
    const amount = VISEME_OPEN_AMOUNT[visemeId] ?? 0;
    return { [_fallbackMorphName]: amount };
  }
  return {};
}

/** 将权重 map 写入模型 morphTargetInfluences */
function _applyWeights(weights) {
  if (!avatarModel) return;

  if (_morphMode === 'arkit') {
    avatarModel.traverse(child => {
      if (!child.isMesh || !child.morphTargetDictionary || !child.morphTargetInfluences) return;
      const dict = child.morphTargetDictionary;
      const inf = child.morphTargetInfluences;
      MOUTH_ARKIT_NAMES.forEach(name => {
        if (name in dict) inf[dict[name]] = weights[name] ?? 0;
      });
    });
  } else if (_morphMode === 'fallback') {
    const val = weights[_fallbackMorphName] ?? 0;
    _fallbackMeshes.forEach(({ mesh, idx }) => {
      mesh.morphTargetInfluences[idx] = val;
    });
  }
}

/**
 * 立即将嘴型设为指定 visemeId（用于重置/打断）。
 * @param {number} visemeId
 */
function setAvatarMouth(visemeId) {
  if (!avatarModel) return;
  _currentWeights = _buildTargetWeights(visemeId);
  _applyWeights(_currentWeights);
}

// ---------------------- Listener Reaction Animation ----------------------
// audio2face_model 的正确用途：用户说话 → 数字人聆听表情
// 58维 fv（ARKit 52 blendshape + 6 头部姿态）直接驱动 Three.js morphTarget
let listenerAnimTimer = null;

/**
 * ARKit 标准 52 Blendshape 名称，索引 0~51 对应 fv 前 52 维。
 * fv[52]~fv[57] 为头部姿态（headYaw, headPitch, headRoll 等），暂不驱动。
 */
const ARKIT_BLENDSHAPE_NAMES = [
  'eyeBlinkLeft',        // 0
  'eyeLookDownLeft',     // 1
  'eyeLookInLeft',       // 2
  'eyeLookOutLeft',      // 3
  'eyeLookUpLeft',       // 4
  'eyeSquintLeft',       // 5
  'eyeWideLeft',         // 6
  'eyeBlinkRight',       // 7
  'eyeLookDownRight',    // 8
  'eyeLookInRight',      // 9
  'eyeLookOutRight',     // 10
  'eyeLookUpRight',      // 11
  'eyeSquintRight',      // 12
  'eyeWideRight',        // 13
  'jawForward',          // 14
  'jawLeft',             // 15
  'jawRight',            // 16
  'jawOpen',             // 17
  'mouthClose',          // 18
  'mouthFunnel',         // 19
  'mouthPucker',         // 20
  'mouthLeft',           // 21
  'mouthRight',          // 22
  'mouthSmileLeft',      // 23
  'mouthSmileRight',     // 24
  'mouthFrownLeft',      // 25
  'mouthFrownRight',     // 26
  'mouthDimpleLeft',     // 27
  'mouthDimpleRight',    // 28
  'mouthStretchLeft',    // 29
  'mouthStretchRight',   // 30
  'mouthRollLower',      // 31
  'mouthRollUpper',      // 32
  'mouthShrugLower',     // 33
  'mouthShrugUpper',     // 34
  'mouthPressLeft',      // 35
  'mouthPressRight',     // 36
  'mouthLowerDownLeft',  // 37
  'mouthLowerDownRight', // 38
  'mouthUpperUpLeft',    // 39
  'mouthUpperUpRight',   // 40
  'browDownLeft',        // 41
  'browDownRight',       // 42
  'browInnerUp',         // 43
  'browOuterUpLeft',     // 44
  'browOuterUpRight',    // 45
  'cheekPuff',           // 46
  'cheekSquintLeft',     // 47
  'cheekSquintRight',    // 48
  'noseSneerLeft',       // 49
  'noseSneerRight',      // 50
  'tongueOut',           // 51
];

/**
 * 将58维 fv 帧应用到3D模型的 morphTarget（blendshape）。
 * 前52维按 ARKit 标准名称直接映射到 morphTarget 权重。
 * 后6维为头部姿态，暂不处理（可扩展为骨骼旋转）。
 * 若模型不含对应 morphTarget，静默跳过。
 */
function applyFvToAvatar(fv58) {
  if (!avatarModel || !fv58) return;
  avatarModel.traverse(child => {
    if (!child.isMesh || !child.morphTargetDictionary || !child.morphTargetInfluences) return;
    const dict = child.morphTargetDictionary;
    const influences = child.morphTargetInfluences;
    for (let i = 0; i < 52 && i < fv58.length; i++) {
      const name = ARKIT_BLENDSHAPE_NAMES[i];
      if (dict[name] === undefined) continue;
      influences[dict[name]] = Math.max(0, Math.min(1, fv58[i]));
    }
  });
}

/**
 * 按 25fps 逐帧播放聆听反应动画序列。
 * 新的动画序列到来时会覆盖正在播放的旧序列。
 */
function playListenerReactionAnimation(frames, fps) {
  if (listenerAnimTimer !== null) {
    clearInterval(listenerAnimTimer);
    listenerAnimTimer = null;
  }
  if (!frames || frames.length === 0) return;

  let frameIdx = 0;
  const interval = Math.round(1000 / (fps || 25));

  listenerAnimTimer = setInterval(() => {
    if (frameIdx >= frames.length) {
      clearInterval(listenerAnimTimer);
      listenerAnimTimer = null;
      return;
    }
    const frame = frames[frameIdx++];
    applyFvToAvatar(frame.fv);
  }, interval);
}

// ---------------------- Camera & Frame Capture ----------------------
const Camera = {
  stream: null,
  frameTimer: null,
  _canvas: null,
  _ctx: null,

  /** 初始化摄像头：获取流并绑定到 video 元素 */
  async init() {
    const video = document.getElementById('camera');
    if (!video) return;
    // 若已有流则不重复初始化
    if (this.stream) return;
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 320 }, height: { ideal: 240 }, facingMode: 'user' },
        audio: false,
      });
      video.srcObject = this.stream;
      video.play().catch(() => { });
      console.log('[Camera] 摄像头已开启');
      this._canvas = document.createElement('canvas');
      this._canvas.width = 320;
      this._canvas.height = 240;
      this._ctx = this._canvas.getContext('2d');
      this._startFrameLoop(video);
    } catch (err) {
      console.warn('[Camera] 摄像头初始化失败:', err.name, err.message);
      const hint = document.querySelector('.camera-hint');
      if (hint) hint.textContent = '📷 摄像头不可用（' + err.name + '）';
    }
  },

  /** 每秒截一帧，通过 WebSocket 发给后端做情绪识别 */
  _startFrameLoop(video) {
    if (this.frameTimer) clearInterval(this.frameTimer);
    this.frameTimer = setInterval(() => {
      if (!state.wsConnected) return;
      if (!video.videoWidth || !video.videoHeight) return;
      try {
        this._ctx.drawImage(video, 0, 0, this._canvas.width, this._canvas.height);
        const dataUrl = this._canvas.toDataURL('image/jpeg', 0.6);
        wsSend({ type: 'frame', data: dataUrl });
      } catch (_) { }
    }, 1000);
  },

  /** 释放摄像头流和定时器 */
  stop() {
    if (this.frameTimer) { clearInterval(this.frameTimer); this.frameTimer = null; }
    if (this.stream) {
      this.stream.getTracks().forEach(t => t.stop());
      this.stream = null;
    }
    const video = document.getElementById('camera');
    if (video) { video.srcObject = null; }
    console.log('[Camera] 摄像头已关闭');
  },
};

// ---------------------- Three.js Scene ----------------------
let avatarScene = null, avatarCamera = null, avatarRenderer = null, avatarModel = null, avatarControls = null;

function initAvatarScene() {
  const container = document.getElementById('digitalHumanArea');
  container.innerHTML = `
    <div id="breathingLight" class="breathing-light"></div>
    <div id="avatarStatus" class="avatar-status"><span id="statusText">👋 在线</span></div>
  `;
  avatarScene = new THREE.Scene();
  avatarScene.background = new THREE.Color(0xe8f0f8);

  avatarCamera = new THREE.PerspectiveCamera(45, container.clientWidth / container.clientHeight, 0.1, 1000);
  avatarCamera.position.set(0, 0.7, 3.6);
  avatarCamera.lookAt(0, 0.5, 0);

  avatarRenderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  avatarRenderer.setPixelRatio(window.devicePixelRatio);
  avatarRenderer.setClearColor(0xe8f0f8, 1);
  if (THREE.sRGBEncoding !== undefined) avatarRenderer.outputEncoding = THREE.sRGBEncoding;
  avatarRenderer.gammaFactor = 2.2;
  avatarRenderer.toneMapping = THREE.ACESFilmicToneMapping;
  avatarRenderer.toneMappingExposure = 1.0;
  container.appendChild(avatarRenderer.domElement);

  avatarControls = new THREE.OrbitControls(avatarCamera, avatarRenderer.domElement);
  avatarControls.target.set(0, 0.5, 0);
  avatarControls.enableZoom = true;
  avatarControls.enablePan = true;
  avatarControls.enableRotate = true;
  avatarControls.zoomSpeed = 0.8;
  avatarControls.update();

  // Lights
  avatarScene.add(new THREE.AmbientLight(0xc8d8e8, 0.65));
  const main = new THREE.DirectionalLight(0xfff5f0, 0.9);
  main.position.set(3, 5, 4);
  avatarScene.add(main);
  [[0xaaccee, 0.4, [-1.5, 1.5, 2]], [0xd0e4f0, 0.35, [0, 1.5, -2]],
  [0xbbd4e8, 0.3, [1.8, 1.2, 1.5]], [0xc8dce8, 0.2, [0, -0.8, 0.5]]
  ].forEach(([color, intensity, pos]) => {
    const l = new THREE.PointLight(color, intensity);
    l.position.set(...pos);
    avatarScene.add(l);
  });

  function updateSize() {
    if (!avatarCamera || !avatarRenderer) return;
    const r = container.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) return;
    avatarCamera.aspect = r.width / r.height;
    avatarCamera.updateProjectionMatrix();
    avatarRenderer.setSize(r.width, r.height);
  }
  window.addEventListener('resize', updateSize);

  (function animate() {
    requestAnimationFrame(animate);
    if (avatarControls) avatarControls.update();
    if (avatarRenderer && avatarScene && avatarCamera) avatarRenderer.render(avatarScene, avatarCamera);
  })();
  requestAnimationFrame(updateSize);
}

function refreshAvatarSize() {
  if (!avatarRenderer) return;
  const c = document.getElementById('digitalHumanArea');
  const r = c.getBoundingClientRect();
  if (r.width === 0 || r.height === 0) { requestAnimationFrame(refreshAvatarSize); return; }
  avatarCamera.aspect = r.width / r.height;
  avatarCamera.updateProjectionMatrix();
  avatarRenderer.setSize(r.width, r.height);
}

function loadAvatarModel(path) {
  return new Promise((resolve, reject) => {
    new THREE.GLTFLoader().load(path, (gltf) => {
      avatarModel = gltf.scene;
      avatarModel.rotation.y = -Math.PI / 2;
      avatarModel.rotation.x = -0.12;

      avatarModel.traverse((child) => {
        if (!child.isMesh) return;
        const mats = Array.isArray(child.material) ? child.material : [child.material];
        mats.forEach(m => {
          if (!m || (!m.isMeshStandardMaterial && !m.isMeshPhongMaterial)) return;
          if (m.color && typeof m.color.getHSL === 'function') {
            const hsl = { h: 0, s: 0, l: 0 };
            m.color.getHSL(hsl);
            m.color.setHSL(hsl.h, Math.max(0, hsl.s - 0.15), Math.min(0.9, hsl.l + 0.05));
          }
          if (m.roughness !== undefined) m.roughness = Math.min(0.6, m.roughness + 0.1);
          if (m.metalness !== undefined) m.metalness = Math.max(0, m.metalness - 0.1);
          m.needsUpdate = true;
        });
      });

      const box = new THREE.Box3().setFromObject(avatarModel);
      const center = box.getCenter(new THREE.Vector3());
      const size = box.getSize(new THREE.Vector3());
      avatarModel.position.set(-center.x, -center.y + size.y / 2, -center.z);
      const scale = 2.0 / Math.max(size.x, size.y, size.z);
      avatarModel.scale.set(scale, scale, scale);
      avatarScene.add(avatarModel);
      buildAdaptiveVisemeMap();
      resolve();
    }, undefined, reject);
  });
}

function destroyAvatar() {
  if (avatarRenderer) { avatarRenderer.dispose(); avatarRenderer = null; }
  avatarScene = null; avatarCamera = null; avatarModel = null; avatarControls = null;
}

// ---------------------- Session Manager ----------------------
function loadSessions() {
  try { state.sessions = JSON.parse(localStorage.getItem('chatbot-sessions') || '[]'); }
  catch (_) { state.sessions = []; }
}

function saveSessions() { localStorage.setItem('chatbot-sessions', JSON.stringify(state.sessions)); }

function getAvatarSessions() {
  return state.avatar ? state.sessions.filter(s => s.avatarId === state.avatar.id) : [];
}

function formatPreview(session) {
  if (!session.messages.length) return '空闲会话';
  const last = session.messages[session.messages.length - 1];
  const text = last.text.length > 24 ? last.text.slice(0, 24) + '...' : last.text;
  return `${last.role === 'user' ? '我' : state.avatar.name}：${text}`;
}

function renderSessionList() {
  dom.sessionList.innerHTML = '';
  const sorted = getAvatarSessions().sort((a, b) => b.updated - a.updated);
  const countEl = document.getElementById('sessionCount');
  if (countEl) countEl.textContent = `(${sorted.length})`;

  sorted.forEach(session => {
    const item = document.createElement('div');
    item.className = `session-item${session.id === state.sessionId ? ' active' : ''}`;
    item.dataset.sessionId = session.id;
    const summary = document.createElement('div');
    summary.className = 'session-summary';
    summary.textContent = session.title || formatPreview(session);
    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'session-delete-btn';
    del.dataset.deleteId = session.id;
    del.innerHTML = '🗑️';
    del.setAttribute('aria-label', '删除会话');
    item.appendChild(summary);
    item.appendChild(del);
    dom.sessionList.appendChild(item);
  });
}

// 事件委托：绑定一次，处理所有会话项的点击和删除
function initSessionListEvents() {
  dom.sessionList.addEventListener('click', e => {
    // 删除按钮
    const delBtn = e.target.closest('.session-delete-btn');
    if (delBtn) {
      e.preventDefault();
      e.stopPropagation();
      const id = delBtn.dataset.deleteId;
      if (id) deleteSession(id);
      return;
    }
    // 会话项
    const item = e.target.closest('.session-item');
    if (item && item.dataset.sessionId) selectSession(item.dataset.sessionId);
  });
}

function newConversation() {
  if (!state.avatar) return;
  const count = getAvatarSessions().length;
  state.sessionId = `${Date.now()}`;
  const session = { id: state.sessionId, avatarId: state.avatar.id, title: `会话 ${count + 1}`, messages: [], updated: Date.now() };
  state.sessions.unshift(session);
  saveSessions();
  renderSessionList();
  dom.messagesContainer.innerHTML = '';

  const welcome = personalGreeting(state.avatar.welcome || '你好，我在这里陪你聊天。');
  addHistory('bot', welcome);
  addMsgToSession('bot', welcome);
  setStatus(STATUS.speaking);
  wsSend({ type: 'new_session', sessionId: state.sessionId });
}

function selectSession(sessionId) {
  state.sessionId = sessionId;
  renderSessionList();
  loadSessionMessages(sessionId);
  wsSend({ type: 'switch_session', sessionId: state.sessionId });
}

function loadSessionMessages(sessionId) {
  dom.messagesContainer.innerHTML = '';
  const session = state.sessions.find(s => s.id === sessionId);
  if (!session) return;
  session.messages.forEach(m => addHistory(m.role, m.text));
  scrollToBottom();
}

function addMsgToSession(role, text) {
  if (!state.sessionId) return;
  const session = state.sessions.find(s => s.id === state.sessionId);
  if (!session) return;
  session.messages.push({ role, text, timestamp: Date.now() });
  session.updated = Date.now();
  const titleChanged = session.messages.length === 2 && role === 'user' && !session.title.includes(text);
  if (titleChanged) {
    session.title = text.length > 15 ? text.slice(0, 15) + '...' : text;
  }
  saveSessions();
  // 仅在标题变化时重新渲染列表，避免频繁重建 DOM 导致删除按钮无法点击
  if (titleChanged) renderSessionList();
}

function deleteSession(sessionId) {
  const targetId = String(sessionId);
  state.sessions = state.sessions.filter(s => String(s.id) !== targetId);
  if (String(state.sessionId) === targetId) {
    const remaining = getAvatarSessions();
    if (remaining.length > 0) { state.sessionId = remaining[0].id; loadSessionMessages(state.sessionId); }
    else { state.sessionId = null; dom.messagesContainer.innerHTML = ''; }
  }
  saveSessions();
  renderSessionList();
}

// ---------------------- Chat ----------------------
function addHistory(role, text) {
  const div = document.createElement('div');
  div.className = `message message-${role}`;
  div.textContent = text;
  dom.messagesContainer.appendChild(div);
  scrollToBottom();
}

function sendMessage() {
  const text = dom.messageInput.value.trim();
  if (!text || !state.avatar) return;
  dom.messageInput.value = '';
  addHistory('user', text);
  addMsgToSession('user', text);
  showThinking(true);
  setStatus(STATUS.thinking);
  // 只有在非 barge_in 场景下才 reset（barge_in_start 已经 reset 过了）
  if (!state.bargeInActive) {
    AudioPlayer.reset();
  }
  state.bargeInActive = false;

  if (state.ws && state.ws.readyState === WebSocket.OPEN) {
    wsSend({ type: 'message', content: text, sessionId: state.sessionId, timestamp: Date.now(), userName: state.userName });
  } else {
    showThinking(false);
    setStatus(STATUS.online);
    const err = '连接服务器失败，请刷新页面重试';
    addHistory('bot', err);
    addMsgToSession('bot', err);
  }
}

function sendQuickPhrase(phrase) {
  dom.messageInput.value = phrase;
  sendMessage();
  dom.messageInput.focus();
}

// ---------------------- UI: CareMode / UserName ----------------------
function toggleCareMode() {
  document.body.classList.toggle('care-mode');
  const on = document.body.classList.contains('care-mode');
  localStorage.setItem('care-mode', on ? 'enabled' : 'disabled');
  showToast(on ? '✅ 已开启关怀模式，文字更大更清晰' : '关怀模式已关闭', 'info');
  setTimeout(() => { if (avatarRenderer) refreshAvatarSize(); }, 100);
}

function applyCareModePreference() {
  if (localStorage.getItem('care-mode') === 'enabled') document.body.classList.add('care-mode');
}

function loadUserName() {
  state.userName = localStorage.getItem('warm-companion-username') || '';
  if (dom.userDisplayName) dom.userDisplayName.textContent = state.userName || '朋友';
  const input = document.getElementById('userNameInput');
  if (input && state.userName) input.value = state.userName;
}

function saveUserName() {
  const input = document.getElementById('userNameInput');
  const name = input.value.trim();
  if (name) {
    state.userName = name;
    localStorage.setItem('warm-companion-username', name);
    if (dom.userDisplayName) dom.userDisplayName.textContent = name;
    showToast(`好的，${name}，我会记住您的名字！`, 'info');
    input.value = name;
  } else {
    showToast('请输入您的称呼', 'warning');
  }
}

function updateTimeGreeting() {
  if (dom.timeGreeting) dom.timeGreeting.textContent = getGreeting();
}

// ---------------------- Voice Recognition ----------------------
let _SRConstructor = null;

function initVoiceRecognition() {
  if (!('webkitSpeechRecognition' in window) && !('SpeechRecognition' in window)) {
    showToast('您的浏览器不支持语音识别功能', 'warning');
    return;
  }
  _SRConstructor = window.SpeechRecognition || window.webkitSpeechRecognition;
}

// 每次录音前创建全新实例，避免实例进入终止状态后无法复用
function createRecognitionInstance() {
  if (!_SRConstructor) return null;
  const rec = new _SRConstructor();
  rec.continuous = false;
  rec.interimResults = false;
  rec.lang = 'zh-CN';
  rec.onstart = () => {
    state.recording = true;
    state.bargeInActive = true; // 标记打断已触发，防止后续 sendMessage 重复 reset
    wsSend({ type: 'barge_in_start', timestamp: Date.now() });
    AudioPlayer.reset();
    setStatus(STATUS.listening);
  };
  rec.onresult = (e) => {
    dom.messageInput.value = e.results[0][0].transcript;
    setTimeout(() => { if (dom.messageInput.value.trim()) sendMessage(); }, 100);
  };
  rec.onerror = (e) => {
    stopVoiceUI();
    setStatus(STATUS.online);
    if (e.error !== 'no-speech') showToast('语音识别失败，请重试', 'warning');
  };
  rec.onend = () => { stopVoiceUI(); setStatus(STATUS.online); };
  return rec;
}

function stopVoiceUI() {
  state.recording = false;
  const btn = document.getElementById('centerVoiceBtn');
  if (btn) btn.classList.remove('recording');
}

function startVoiceRecording() {
  state.pressTimer = setTimeout(() => {
    state.isLongPress = true;
    const btn = document.getElementById('centerVoiceBtn');
    if (btn) { btn.classList.add('recording'); btn.innerHTML = '<span class="voice-icon">🎙️</span> 录音中...'; }
    if (!_SRConstructor) { showToast('您的浏览器不支持语音识别功能', 'warning'); return; }
    // 每次都创建新实例，避免旧实例进入 ended 状态后无法重启
    state.recognition = createRecognitionInstance();
    try { state.recognition.start(); } catch (_) { showToast('语音识别暂时不可用', 'warning'); }
  }, 300);
  const btn = document.getElementById('centerVoiceBtn');
  if (btn) btn.classList.add('pressing');
}

function stopVoiceRecording() {
  clearTimeout(state.pressTimer);
  const btn = document.getElementById('centerVoiceBtn');
  if (btn) btn.classList.remove('pressing');

  if (state.isLongPress) {
    state.isLongPress = false;
    if (btn) { btn.classList.remove('recording'); btn.innerHTML = '<span class="voice-icon">🎙️</span> 按住说话'; }
    if (state.recognition) { try { state.recognition.stop(); } catch (_) { stopVoiceUI(); } }
  } else {
    dom.messageInput.placeholder = '直接输入或长按说话';
    showToast('💡 长按按钮可以语音输入哦', 'info');
  }
}

// ---------------------- Page Navigation ----------------------
async function selectAvatar(id) {
  state.avatar = AVATARS[id];
  dom.botName.textContent = state.avatar.name;
  dom.botDesc.textContent = state.avatar.desc;
  dom.avatarContainer.className = `avatar-image ${state.avatar.skinClass}`;
  dom.avatarContainer.textContent = state.avatar.icon;
  destroyAvatar();
  try { initAvatarScene(); await loadAvatarModel(state.avatar.modelPath); }
  catch (_) { showToast('数字人加载失败，请稍后重试', 'warning'); }

  dom.selectPage.classList.remove('active');
  dom.chatPage.classList.add('active');
  loadSessions();
  loadUserName();

  const avatarSessions = getAvatarSessions();
  if (avatarSessions.length === 0) {
    newConversation();
  } else {
    if (!state.sessionId || !avatarSessions.some(s => s.id === state.sessionId)) {
      state.sessionId = avatarSessions[0].id;
    }
    renderSessionList();
    loadSessionMessages(state.sessionId);
  }
  refreshAvatarSize();
  connectWebSocket();
  Camera.init();
  dom.messageInput.focus();
  setStatus(STATUS.online);
}

function goBack() {
  state.reconnects = MAX_RECONNECT;
  if (state.ws) state.ws.close();
  AudioPlayer.reset();
  resetBotBubbleState();
  Camera.stop();
  dom.chatPage.classList.remove('active');
  dom.selectPage.classList.add('active');
  destroyAvatar();
  state.avatar = null;
  dom.messagesContainer.innerHTML = '';
}

// ---------------------- Init ----------------------
window.addEventListener('DOMContentLoaded', () => {
  cacheDom();
  applyCareModePreference();
  loadUserName();
  updateTimeGreeting();
  setInterval(updateTimeGreeting, 60000);
  initVoiceRecognition();
  initSessionListEvents();

  dom.sendBtn.addEventListener('click', sendMessage);
  dom.messageInput.addEventListener('keydown', e => {
    if (e.key === 'Enter') { e.preventDefault(); sendMessage(); }
  });
});

// Global exports for HTML onclick handlers
window.selectAvatar = selectAvatar;
window.goBack = goBack;
window.newConversation = newConversation;
window.sendQuickPhrase = sendQuickPhrase;
window.toggleCareMode = toggleCareMode;
window.saveUserName = saveUserName;
window.startVoiceRecording = startVoiceRecording;
window.stopVoiceRecording = stopVoiceRecording;
