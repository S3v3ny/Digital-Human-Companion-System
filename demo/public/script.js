// ============================================================
// script.js - TalkingHead.js 集成版（ES module）
// ============================================================

// ---------------------- CONFIG ----------------------
const AVATARS = [
  null,
  { id: 1, name: '小丽', desc: '温柔可爱，陪你聊天～', icon: '👧', skinClass: 'avatar-friend1', welcome: '你好呀～我是小丽，很高兴认识你！', modelPath: '/static/female-avatar1.glb' },
  { id: 2, name: '老王', desc: '风趣幽默，随时唠嗑～', icon: '👴', skinClass: 'avatar-friend2', welcome: '你好，我是老王，咱们随便聊！', modelPath: '/static/3d卡通老人头部模型.glb' },
  { id: 3, name: '小明', desc: '年轻伙伴，活力陪聊～', icon: '🧑', skinClass: 'avatar-friend3', welcome: '你好，我是小明，和你聊聊生活、兴趣、好心情！', modelPath: '/static/3d卡通少年头部模型.glb' },
];

const STATUS = {
  online:    { key: 'online',        text: '👋 在线' },
  thinking:  { key: 'thinking',      text: '🤔 思考中...' },
  speaking:  { key: 'speaking',      text: '💬 正在说话' },
  playAudio: { key: 'speaking',      text: '🔊 播放语音' },
  listening: { key: 'listening',     text: '🎤 正在听您说话...' },
  listenSay: { key: 'listening',     text: '🎤 我在听，您说' },
  offline:   { key: 'offline',       text: '⚠️ 离线' },
  offlineD:  { key: 'offline',       text: '⚠️ 连接断开' },
  reconnect: { key: 'reconnecting',  text: '🔄 重连中...' },
  genReply:  { key: 'thinking',      text: '⚡ 正在生成回复...' },
};

const MAX_RECONNECT = 5;

// ---------------------- state ----------------------
const state = {
  avatar: null, sessionId: null, sessions: [], userName: '',
  ws: null, wsConnected: false, reconnects: 0,
  recording: false, recognition: null,
  responseId: null, botChunks: [],
  currentChunkEl: null,
  thinkingEl: null, pressTimer: null, isLongPress: false,
  bargeInActive: false,
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
  if (h >= 5 && h < 9)  return '🌅 早上好';
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

// ---------------------- TalkingHead instance ----------------------
let talkingHead = null;

// ---------------------- HeadAudio (real-time lipsync) ----------------------
let headAudio = null;
let _haRAF = null;
let _haLastTime = 0;
let _haDelayNode = null;

async function initHeadAudio() {
  if (!talkingHead) return;
  try {
    const { HeadAudio } = await import('/static/headaudio.mjs');
    await talkingHead.audioCtx.audioWorklet.addModule('/static/headworklet.mjs');

    headAudio = new HeadAudio(talkingHead.audioCtx, {
      processorOptions: { vadEventsEnabled: false }
    });
    await headAudio.loadModel('/static/model-en-mixed.bin');

    // Tap speech signal for analysis (before delay)
    talkingHead.audioSpeechGainNode.connect(headAudio);

    // Insert 150ms delay so audio plays AFTER the lip prediction arrives
    _haDelayNode = new DelayNode(talkingHead.audioCtx, { delayTime: 0.15 });
    talkingHead.audioSpeechGainNode.disconnect(talkingHead.audioReverbNode);
    talkingHead.audioSpeechGainNode.connect(_haDelayNode);
    _haDelayNode.connect(talkingHead.audioReverbNode);

    // Push eased viseme values into TalkingHead's morph target system
    headAudio.onvalue = (key, value) => {
      if (talkingHead?.mtAvatar?.hasOwnProperty(key)) {
        Object.assign(talkingHead.mtAvatar[key], { newvalue: value, needsUpdate: true });
      }
    };

    // HeadAudio needs its own update loop for easing fade-in/out
    _haLastTime = performance.now();
    function _haLoop(t) {
      if (!headAudio) return;
      headAudio.update(t - _haLastTime);
      _haLastTime = t;
      _haRAF = requestAnimationFrame(_haLoop);
    }
    _haRAF = requestAnimationFrame(_haLoop);
    console.log('[HeadAudio] real-time lipsync ready');
  } catch (e) {
    console.warn('[HeadAudio] init failed (falling back to timestamp lipsync):', e);
  }
}

function destroyHeadAudio() {
  if (_haRAF !== null) { cancelAnimationFrame(_haRAF); _haRAF = null; }
  if (headAudio && talkingHead) {
    try { talkingHead.audioSpeechGainNode.disconnect(headAudio); } catch (_) {}
    try {
      if (_haDelayNode) {
        talkingHead.audioSpeechGainNode.disconnect(_haDelayNode);
        talkingHead.audioSpeechGainNode.connect(talkingHead.audioReverbNode);
      }
    } catch (_) {}
  }
  headAudio = null;
  _haDelayNode = null;
}

// Azure TTS viseme ID (0-21) → OVR/TalkingHead viseme name
// Silence (id=0) maps to null and is skipped.
const _AZURE_TO_OVR = [
  null,  // 0  silence
  'aa',  // 1  æ ə ʌ
  'aa',  // 2  ɑ
  'O',   // 3  ɔ
  'E',   // 4  eɪ
  'I',   // 5  ɪ
  'U',   // 6  ʊ w
  'U',   // 7  uː
  'O',   // 8  oʊ
  'aa',  // 9  aʊ
  'O',   // 10 ɔɪ
  'aa',  // 11 aɪ
  'CH',  // 12 h
  'RR',  // 13 ɹ
  'nn',  // 14 l
  'SS',  // 15 s z
  'CH',  // 16 ʃ tʃ dʒ
  'TH',  // 17 θ ð
  'FF',  // 18 f v
  'DD',  // 19 d t n
  'kk',  // 20 k g
  'PP',  // 21 p b m
];

// ---------------------- AudioPlayer (TalkingHead-backed) ----------------------
// TalkingHead's playAudio() requires an AudioBuffer, NOT a data-URL string.
// We decode base64 → ArrayBuffer → AudioBuffer asynchronously, then flush
// in sequence-order once each chunk is ready.
const AudioPlayer = {
  _pending: [],  // {seq, visemes, buffer, ready}
  playing: false,
  responseId: null,
  get queue() { return this._pending; },

  enqueue(base64Audio, responseId, seq = 0, visemes = []) {
    this.responseId = responseId;
    const entry = { seq: Number(seq) || 0, visemes, buffer: null, ready: false };
    this._pending.push(entry);
    this._pending.sort((a, b) => a.seq - b.seq);

    if (!talkingHead?.audioCtx) return;
    const bytes = Uint8Array.from(atob(base64Audio), c => c.charCodeAt(0));
    talkingHead.audioCtx.decodeAudioData(bytes.buffer.slice(0))
      .then(buf => { entry.buffer = buf; entry.ready = true; this._flush(); })
      .catch(e => { console.error('[Audio] decode failed', e); entry.ready = true; this._flush(); });
  },

  _flush() {
    if (!talkingHead) { this._pending = []; return; }
    // Process only front-of-queue items that are ready (preserve order).
    // Visemes are intentionally omitted — HeadAudio drives lipsync in real-time
    // by analysing the audio signal directly, which is more accurate than
    // Azure's pre-computed timestamps.
    while (this._pending.length > 0 && this._pending[0].ready) {
      const { buffer } = this._pending.shift();
      if (!buffer) continue;
      this.playing = true;
      talkingHead.speakAudio({ audio: buffer });
    }
  },

  reset() {
    this._pending = [];
    this.playing = false;
    this.responseId = null;
    if (talkingHead) { try { talkingHead.stopSpeaking(); } catch (_) {} }
  },
};

// ---------------------- Bot Message Rendering ----------------------
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

function updateCurrentBubble(responseId, text) {
  if (state.responseId !== responseId) {
    state.responseId = responseId;
    state.botChunks = [];
    state.currentChunkEl = null;
  }
  if (!state.currentChunkEl) return createBotBubble(responseId, text);
  state.currentChunkEl.textContent = text;
  state.botChunks[state.botChunks.length - 1] = text;
  scrollToBottom();
}

function getBotFullText() { return state.botChunks.join(''); }

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

// ---------------------- Emotion (TalkingHead moods) ----------------------
const _MOOD_MAP = {
  happy: 'happy', sad: 'sad', surprise: 'surprised',
  caring: 'love', neutral: 'neutral',
};

function setAvatarEmotion(key) {
  if (talkingHead) talkingHead.setMood(_MOOD_MAP[key] || 'neutral');
}

function clearAvatarEmotion() {
  if (talkingHead) talkingHead.setMood('neutral');
}

// ---------------------- Viseme / animation stubs ----------------------
// TalkingHead handles lipsync and idle animation internally.
function clearVisemeTimers() {}
function setAvatarMouth(v) {}
let listenerAnimTimer = null;
function playListenerReactionAnimation(frames, fps) {}
async function startMicLipSync() {}
function stopMicLipSync() {}
function refreshAvatarSize() {}

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
  if (chunkText && !state.botChunks.includes(chunkText)) createBotBubble(rid, chunkText);
  if (d.emotion) setAvatarEmotion(d.emotion);
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
    const delta = d.delta || d.text || '';
    if (delta) createBotBubble(rid, delta);
  },
  listener_reaction(d) {
    const frames = d.frames || [];
    const fps = d.fps || 25;
    if (frames.length) playListenerReactionAnimation(frames, fps);
  },
  turn_start(d) {
    showThinking(false);
    if (listenerAnimTimer !== null) { clearInterval(listenerAnimTimer); listenerAnimTimer = null; }
    clearVisemeTimers();
    setAvatarMouth(0);
    clearAvatarEmotion();
    AudioPlayer.reset();
    resetBotBubbleState();
    state.responseId = d.responseId || `${Date.now()}`;
    setStatus(STATUS.genReply);
  },
  stop_output(d) {
    clearVisemeTimers();
    setAvatarMouth(0);
    AudioPlayer.reset();
    showThinking(false);
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
    const spokenText = d.spokenText || getBotFullText();
    if (spokenText) addMsgToSession('bot', spokenText + '…');
    resetBotBubbleState();
    state.responseId = null;
    setStatus(STATUS.listenSay);
  },
  listen_state() { setStatus(STATUS.listenSay); },
  turn_end(d) {
    showThinking(false);
    const fullText = d.fullText || getBotFullText();
    if (fullText) addMsgToSession('bot', fullText);
    resetBotBubbleState();
    state.responseId = null;
    clearAvatarEmotion();
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

// ---------------------- Camera & Frame Capture ----------------------
const Camera = {
  stream: null,
  frameTimer: null,
  _canvas: null,
  _ctx: null,

  async init() {
    const video = document.getElementById('camera');
    if (!video || this.stream) return;
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        video: { width: { ideal: 320 }, height: { ideal: 240 }, facingMode: 'user' },
        audio: false,
      });
      video.srcObject = this.stream;
      video.play().catch(() => {});
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

  _startFrameLoop(video) {
    if (this.frameTimer) clearInterval(this.frameTimer);
    this.frameTimer = setInterval(() => {
      if (!state.wsConnected || !video.videoWidth || !video.videoHeight) return;
      try {
        this._ctx.drawImage(video, 0, 0, this._canvas.width, this._canvas.height);
        wsSend({ type: 'frame', data: this._canvas.toDataURL('image/jpeg', 0.6) });
      } catch (_) {}
    }, 1000);
  },

  stop() {
    if (this.frameTimer) { clearInterval(this.frameTimer); this.frameTimer = null; }
    if (this.stream) { this.stream.getTracks().forEach(t => t.stop()); this.stream = null; }
    const video = document.getElementById('camera');
    if (video) video.srcObject = null;
  },
};

// ---------------------- TalkingHead Avatar ----------------------
async function initAndLoadAvatar(avatar) {
  const container = document.getElementById('digitalHumanArea');
  container.style.position = 'relative';
  container.innerHTML = '';

  // TalkingHead renders into this wrapper (behind overlay elements)
  const wrapper = document.createElement('div');
  wrapper.style.cssText = 'position:absolute;top:0;left:0;width:100%;height:100%;overflow:hidden;';
  container.appendChild(wrapper);

  // Status overlay elements on top
  container.insertAdjacentHTML('beforeend', `
    <div id="breathingLight" class="breathing-light"></div>
    <div id="avatarStatus" class="avatar-status"><span id="statusText">👋 在线</span></div>
  `);

  // Dynamic import so a CDN failure doesn't break page navigation
  const { TalkingHead } = await import('talkinghead');

  // ttsEndpoint is required by TalkingHead even if unused;
  // we drive audio via speakAudio() so this endpoint is never called.
  talkingHead = new TalkingHead(wrapper, {
    ttsEndpoint: '/api/tts-noop',
    cameraView: 'upper',
    modelPixelRatio: Math.min(window.devicePixelRatio || 1, 2),
    lipsyncLang: 'zh',
  });

  await talkingHead.showAvatar({
    url: avatar.modelPath,
    body: 'F',
    avatarMood: 'neutral',
    lipsyncLang: 'zh',
  });

  // Wire up real-time audio-driven lipsync via HeadAudio
  await initHeadAudio();
}

function destroyAvatar() {
  destroyHeadAudio();
  if (talkingHead) {
    try { talkingHead.stopSpeaking(); } catch (_) {}
    talkingHead = null;
  }
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

function initSessionListEvents() {
  dom.sessionList.addEventListener('click', e => {
    const delBtn = e.target.closest('.session-delete-btn');
    if (delBtn) {
      e.preventDefault();
      e.stopPropagation();
      const id = delBtn.dataset.deleteId;
      if (id) deleteSession(id);
      return;
    }
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
  if (titleChanged) session.title = text.length > 15 ? text.slice(0, 15) + '...' : text;
  saveSessions();
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
  if (!state.bargeInActive) AudioPlayer.reset();
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

function createRecognitionInstance() {
  if (!_SRConstructor) return null;
  const rec = new _SRConstructor();
  rec.continuous = false;
  rec.interimResults = false;
  rec.lang = 'zh-CN';
  rec.onstart = () => {
    state.recording = true;
    state.bargeInActive = true;
    wsSend({ type: 'barge_in_start', timestamp: Date.now() });
    AudioPlayer.reset();
    setStatus(STATUS.listening);
    startMicLipSync();
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
  stopMicLipSync();
}

function startVoiceRecording() {
  state.pressTimer = setTimeout(() => {
    state.isLongPress = true;
    const btn = document.getElementById('centerVoiceBtn');
    if (btn) { btn.classList.add('recording'); btn.innerHTML = '<span class="voice-icon">🎙️</span> 录音中...'; }
    if (!_SRConstructor) { showToast('您的浏览器不支持语音识别功能', 'warning'); return; }
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
  try { await initAndLoadAvatar(state.avatar); }
  catch (e) {
    const msg = e?.message || String(e);
    showToast('加载失败: ' + msg.slice(0, 80), 'warning');
    console.error('[Avatar error]', e);
  }

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
