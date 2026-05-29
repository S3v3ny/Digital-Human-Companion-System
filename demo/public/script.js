// ============================================================
// script.js - TalkingHead.js 集成版（ES module）
// ============================================================

// ---------------------- CONFIG ----------------------
const AVATARS = [
  null,
  { id: 1, name: '小丽', desc: '温柔可爱，陪你聊天～', icon: '👧', skinClass: 'avatar-friend1', welcome: '你好呀～我是小丽，很高兴认识你！', modelPath: '/static/female-avatar1.glb', imagePath: '/static/avatar-xiaoli.png?v=3' },
  { id: 2, name: '老王', desc: '风趣幽默，随时唠嗑～', icon: '👴', skinClass: 'avatar-friend2', welcome: '你好，我是老王，咱们随便聊！', modelPath: '/static/laowang-avatar.glb', imagePath: '/static/avatar-laowang.png?v=3' },
  { id: 3, name: '小明', desc: '年轻伙伴，活力陪聊～', icon: '🧑', skinClass: 'avatar-friend3', welcome: '你好，我是小明，和你聊聊生活、兴趣、好心情！', modelPath: '/static/male-avatar1.glb', imagePath: '/static/avatar-xiaoming.png?v=3' },
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
  userId: '',   // 当前用户的稳定 ID（由前端生成，永久保存在 localStorage）
  ws: null, wsConnected: false, reconnects: 0,
  recording: false, recognition: null,
  responseId: null, botChunks: [],
  currentChunkEl: null,
  thinkingEl: null, pressTimer: null, isLongPress: false,
  bargeInActive: false,
  reminders: [],
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

function getGreetingInfo() {
  const h = new Date().getHours();
  if (h >= 5 && h < 9)  return { text: '早上好', icon: 'sunrise' };
  if (h >= 9 && h < 12) return { text: '上午好', icon: 'sun' };
  if (h >= 12 && h < 14) return { text: '中午好', icon: 'sun' };
  if (h >= 14 && h < 18) return { text: '下午好', icon: 'cloud-sun' };
  if (h >= 18 && h < 22) return { text: '晚上好', icon: 'moon' };
  return { text: '夜深了', icon: 'moon-star' };
}

// 兼容旧调用（如 personalGreeting）
function getGreeting() { return getGreetingInfo().text; }

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
  const wrapper = document.createElement('div');
  wrapper.className = 'message message-bot';
  wrapper.dataset.responseId = String(responseId);
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.textContent = text;
  wrapper.appendChild(bubble);
  dom.messagesContainer.appendChild(wrapper);
  state.currentChunkEl = bubble;
  state.botChunks.push(text);
  scrollToBottom();
  return wrapper;
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
      state.thinkingEl.className = 'message message-bot message-thinking';
      const bubble = document.createElement('div');
      bubble.className = 'bubble';
      bubble.innerHTML = '<span class="thinking-dots">🤔 正在思考</span>';
      state.thinkingEl.appendChild(bubble);
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

// 用户语音情感 → 数字人表情（陪伴式：不镜像负面情绪，转为关切）
const _USER_EMPATHY_MAP = {
  happy: 'happy', excited: 'happy',
  sad: 'love', fear: 'love', frustrated: 'sad',
  angry: 'sad', disgust: 'neutral',
  surprise: 'surprised',
  neutral: 'neutral', bored: 'neutral',
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
  user_emotion(d) {
    const mood = _USER_EMPATHY_MAP[d.emotion] || 'neutral';
    if (talkingHead) talkingHead.setMood(mood);
  },
  turn_start(d) {
    showThinking(false);
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
  reminder_list(d) {
    console.log('[reminder] list received', d);
    state.reminders = Array.isArray(d.reminders) ? d.reminders.slice() : [];
    renderReminders();
  },
  reminder_added(d) {
    console.log('[reminder] added received', d);
    if (!d.reminder) return;
    state.reminders = state.reminders.filter(r => r.id !== d.reminder.id);
    state.reminders.push(d.reminder);
    renderReminders();
    showToast(`已设定提醒：${d.reminder.whenStr} ${d.reminder.content}`, 'info');
  },
  reminder_fired(d) {
    console.log('[reminder] fired received', d);
    if (!d.reminder) return;
    const r = state.reminders.find(x => x.id === d.reminder.id);
    if (r) { r.fired = true; } else { state.reminders.push({ ...d.reminder, fired: true }); }
    renderReminders();
  },
};

function handleServerMessage(data) {
  if (!data || !data.type) return;
  const handler = msgHandlers[data.type];
  if (handler) handler(data);
}

function connectWebSocket() {
  // 已在连接中或已连接时跳过，防止重复建立连接
  if (state.ws && (state.ws.readyState === WebSocket.CONNECTING || state.ws.readyState === WebSocket.OPEN)) {
    return;
  }

  const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
  state.ws = new WebSocket(`${protocol}//${location.host}/ws/chat`);

  state.ws.onopen = () => {
    state.wsConnected = true;
    state.reconnects = 0;
    setStatus(STATUS.online);
    // 只有已选择头像（在聊天页）才发 init，选头像页静默保活
    if (state.avatar) {
      wsSend({ type: 'init', avatarId: state.avatar.id, avatarName: state.avatar.name, sessionId: state.sessionId, userName: state.userName, userId: state.userId, city: localStorage.getItem('warm-companion-city') || '' });
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
    } else if (state.avatar) {
      // 只在聊天页才提示连接失败，选头像页静默重试
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

  // 强制锁定上半身视角，并微调让画面更居中、更近一些
  try {
    talkingHead.setView('upper', {
      cameraDistance: -1.0,  // 相机更靠近 → 主体变大
      cameraY: 0.15,         // look-at 略下移 → 头部从画面顶部移到更居中位置
      cameraX: 0,            // 水平居中
    });
  } catch (e) { console.warn('setView failed', e); }

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

function formatRelativeTime(timestamp) {
  const d = new Date(timestamp);
  const now = new Date();
  const isToday = d.toDateString() === now.toDateString();
  const yesterday = new Date(now.getTime() - 86400000);
  const isYesterday = d.toDateString() === yesterday.toDateString();
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  if (isToday) return `今天 ${hh}:${mm}`;
  if (isYesterday) return `昨天 ${hh}:${mm}`;
  return `${d.getMonth() + 1}月${d.getDate()}日`;
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

    const content = document.createElement('div');
    content.className = 'session-content';

    const time = document.createElement('div');
    time.className = 'session-time';
    time.textContent = formatRelativeTime(session.updated);

    const summary = document.createElement('div');
    summary.className = 'session-summary';
    summary.textContent = session.title || formatPreview(session);

    content.appendChild(time);
    content.appendChild(summary);

    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'session-delete-btn';
    del.dataset.deleteId = session.id;
    del.innerHTML = '🗑️';
    del.setAttribute('aria-label', '删除会话');

    item.appendChild(content);
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
  wsSend({ type: 'new_session', sessionId: state.sessionId, userName: state.userName, userId: state.userId });
}

function selectSession(sessionId) {
  state.sessionId = sessionId;
  renderSessionList();
  loadSessionMessages(sessionId);
  wsSend({ type: 'switch_session', sessionId: state.sessionId, userName: state.userName, userId: state.userId });
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
  const wrapper = document.createElement('div');
  wrapper.className = `message message-${role}`;
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.textContent = text;
  wrapper.appendChild(bubble);
  dom.messagesContainer.appendChild(wrapper);
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
    wsSend({ type: 'message', content: text, sessionId: state.sessionId, timestamp: Date.now(), userName: state.userName, userId: state.userId, city: localStorage.getItem('warm-companion-city') || '' });
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
  const on = !document.body.classList.contains('care-mode');
  document.body.classList.toggle('care-mode', on);
  document.documentElement.classList.toggle('care-mode', on);
  localStorage.setItem('care-mode', on ? 'enabled' : 'disabled');
  showToast(on ? '✅ 已开启关怀模式，文字更大更清晰' : '关怀模式已关闭', 'info');
}

function applyCareModePreference() {
  if (localStorage.getItem('care-mode') === 'enabled') {
    document.body.classList.add('care-mode');
    document.documentElement.classList.add('care-mode');
  }
}

// ---------------------- Reminders ----------------------
function renderReminders() {
  const listEl = document.getElementById('reminderList');
  const emptyHint = document.getElementById('reminderEmptyHint');
  const countEl = document.getElementById('reminderCount');
  console.log('[reminder] render', { count: state.reminders.length, listEl: !!listEl, emptyHint: !!emptyHint });
  if (!listEl || !emptyHint) return;

  const active = state.reminders.filter(r => !r.fired);
  const fired = state.reminders.filter(r => r.fired);
  const ordered = [...fired, ...active]; // 已触发的高亮放最上面
  listEl.innerHTML = '';

  if (state.reminders.length === 0) {
    emptyHint.classList.remove('hidden');
    if (countEl) countEl.classList.add('hidden');
    return;
  }
  emptyHint.classList.add('hidden');
  if (countEl) {
    countEl.textContent = active.length > 0 ? `${active.length} 条待提醒` : '';
    countEl.classList.toggle('hidden', active.length === 0);
  }

  for (const r of ordered) {
    const li = document.createElement('li');
    const baseClass = 'flex items-start gap-2 px-3 py-2 rounded-xl border';
    li.className = r.fired
      ? `${baseClass} bg-warm-100 border-warm-300 animate-pulse`
      : `${baseClass} bg-white/80 border-cream-200`;
    li.innerHTML = `
      <i data-lucide="${r.fired ? 'bell-ring' : 'alarm-clock'}" class="text-warm-600 mt-0.5 shrink-0" style="width:18px;height:18px;"></i>
      <div class="flex-1 min-w-0">
        <div class="text-xs text-warm-700 font-semibold">${r.whenStr}</div>
        <div class="text-sm text-ink-800 break-words">${escapeHtml(r.content)}</div>
      </div>
      <button onclick="dismissReminder('${r.id}')" class="text-ink-400 hover:text-ink-700 shrink-0" title="移除">
        <i data-lucide="x" style="width:14px;height:14px;"></i>
      </button>
    `;
    listEl.appendChild(li);
  }
  if (window.lucide) window.lucide.createIcons();
}

function escapeHtml(s) {
  return String(s || '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

function dismissReminder(id) {
  state.reminders = state.reminders.filter(r => r.id !== id);
  renderReminders();
}
window.dismissReminder = dismissReminder;

// =====================================================================
// 用户系统（本地多用户，画像跨对话持久化）
// =====================================================================
const USERS_KEY    = 'warm-companion-users';
const CUR_USER_KEY = 'warm-companion-current-user';

function loadUsers() {
  try { return JSON.parse(localStorage.getItem(USERS_KEY) || '[]'); }
  catch (_) { return []; }
}

function saveUsers(users) {
  localStorage.setItem(USERS_KEY, JSON.stringify(users));
}

function getCurrentUser() {
  const id = localStorage.getItem(CUR_USER_KEY);
  return loadUsers().find(u => u.id === id) || null;
}

function _applyUser(user) {
  if (!user) return;
  state.userId   = user.id;
  state.userName = user.name;
  localStorage.setItem(CUR_USER_KEY, user.id);

  // 顶栏问候
  if (dom.userDisplayName) dom.userDisplayName.textContent = user.name;

  // 侧边栏
  const nameEl   = document.getElementById('currentUserDisplay');
  const avatarEl = document.getElementById('currentUserAvatar');
  if (nameEl)   nameEl.textContent   = user.name;
  if (avatarEl) avatarEl.textContent = user.name.charAt(0);
}

function selectUser(userId) {
  const users = loadUsers();
  const user  = users.find(u => u.id === userId);
  if (!user) return;
  // 更新 lastActiveAt
  user.lastActiveAt = Date.now();
  saveUsers(users);
  _applyUser(user);
  renderSelectPageUserBar();
  renderUserModalList();
  hideUserModal();
}

function confirmNewUser() {
  const input = document.getElementById('newUserNameInput');
  const name  = (input ? input.value : '').trim();
  if (!name) { showToast('请输入名字', 'warning'); return; }

  const users  = loadUsers();
  const exists = users.find(u => u.name === name);
  if (exists) { selectUser(exists.id); if (input) input.value = ''; return; }

  const newUser = { id: String(Date.now()), name, createdAt: Date.now(), lastActiveAt: Date.now() };
  users.push(newUser);
  saveUsers(users);
  if (input) input.value = '';
  _applyUser(newUser);
  renderSelectPageUserBar();
  renderUserModalList();
  hideUserModal();
  showToast(`欢迎，${name}！个人记忆已为您开启`, 'info');
}

window.deleteUser = function(userId) {
  const users = loadUsers();
  const user  = users.find(u => u.id === userId);
  if (!user) return;
  if (!confirm(`确定删除用户「${user.name}」及其所有记忆记录吗？`)) return;
  const newUsers = users.filter(u => u.id !== userId);
  saveUsers(newUsers);
  // 通知后端删除服务器端画像
  wsSend({ type: 'delete_user', userId });
  if (state.userId === userId) {
    if (newUsers.length > 0) {
      _applyUser(newUsers[0]);
    } else {
      state.userId   = '';
      state.userName = '';
      localStorage.removeItem(CUR_USER_KEY);
      if (dom.userDisplayName) dom.userDisplayName.textContent = '朋友';
      const nameEl   = document.getElementById('currentUserDisplay');
      const avatarEl = document.getElementById('currentUserAvatar');
      if (nameEl)   nameEl.textContent   = '未选择用户';
      if (avatarEl) avatarEl.textContent = '?';
    }
  }
  renderSelectPageUserBar();
  renderUserModalList();
};

function renderSelectPageUserBar() {
  const bar = document.getElementById('selectPageUserBar');
  if (!bar) return;
  bar.innerHTML = '';
  const users = loadUsers();

  if (users.length === 0) {
    bar.innerHTML = `
      <div class="text-center w-full">
        <p class="text-ink-500 text-sm mb-2">首次使用，请先告诉我您的名字</p>
        <button onclick="showUserModal()"
          class="px-6 py-2.5 bg-warm-500 hover:bg-warm-600 text-white rounded-full font-medium shadow-md transition">
          创建我的账号
        </button>
      </div>`;
    if (window.lucide) window.lucide.createIcons();
    return;
  }

  users.sort((a, b) => (b.lastActiveAt || 0) - (a.lastActiveAt || 0)).forEach(u => {
    const btn = document.createElement('button');
    btn.className = 'user-chip ' + (u.id === state.userId ? 'selected' : 'unselected');
    btn.textContent = u.name;
    btn.onclick = () => selectUser(u.id);
    bar.appendChild(btn);
  });

  const addBtn = document.createElement('button');
  addBtn.className = 'user-chip add-btn';
  addBtn.textContent = '＋ 新用户';
  addBtn.onclick = showUserModal;
  bar.appendChild(addBtn);
}

function renderUserModalList() {
  const listEl = document.getElementById('userModalList');
  if (!listEl) return;
  listEl.innerHTML = '';
  const users = loadUsers();
  if (users.length === 0) {
    listEl.innerHTML = '<p class="text-sm text-ink-400 text-center py-3">还没有用户，请在下方创建</p>';
    return;
  }
  users.sort((a, b) => (b.lastActiveAt || 0) - (a.lastActiveAt || 0)).forEach(u => {
    const isActive = u.id === state.userId;
    const div = document.createElement('div');
    div.className = 'user-list-item' + (isActive ? ' active' : '');
    div.innerHTML = `
      <div class="flex items-center gap-2.5 min-w-0">
        <div class="w-9 h-9 rounded-full flex-shrink-0 flex items-center justify-center font-bold text-sm
          ${isActive ? 'bg-warm-500 text-white' : 'bg-cream-200 text-ink-700'}">
          ${escapeHtml(u.name.charAt(0))}
        </div>
        <div class="min-w-0">
          <div class="font-medium text-ink-900 text-sm truncate">${escapeHtml(u.name)}</div>
          ${isActive ? '<div class="text-xs text-warm-600">当前用户</div>' : ''}
        </div>
      </div>
      <div class="flex items-center gap-1.5 flex-shrink-0">
        ${!isActive ? `<button onclick="selectUser('${u.id}')"
          class="text-xs px-3 py-1.5 bg-warm-500 hover:bg-warm-600 text-white rounded-lg font-medium transition">选择</button>` : ''}
        <button onclick="deleteUser('${u.id}')"
          class="text-xs px-2.5 py-1.5 text-danger-500 hover:bg-danger-50 rounded-lg transition">删除</button>
      </div>`;
    listEl.appendChild(div);
  });
}

function showUserModal() {
  const modal = document.getElementById('userModal');
  if (modal) { modal.classList.add('active'); renderUserModalList(); }
  if (window.lucide) window.lucide.createIcons();
  setTimeout(() => {
    const inp = document.getElementById('newUserNameInput');
    if (inp) inp.focus();
  }, 100);
}

function hideUserModal() {
  const modal = document.getElementById('userModal');
  if (modal) modal.classList.remove('active');
}

function initUserSystem() {
  const users  = loadUsers();
  const curId  = localStorage.getItem(CUR_USER_KEY);
  const oldName = localStorage.getItem('warm-companion-username');

  // 迁移旧版单用户名字到新用户系统
  if (users.length === 0 && oldName) {
    const migrated = { id: String(Date.now()), name: oldName, createdAt: Date.now(), lastActiveAt: Date.now() };
    saveUsers([migrated]);
    _applyUser(migrated);
  } else {
    const cur = users.find(u => u.id === curId) || (users.length > 0 ? users[0] : null);
    if (cur) _applyUser(cur);
  }

  renderSelectPageUserBar();
}

// =====================================================================
// (旧函数保留为空 stub，防止 HTML 里已有 onclick 报错)
// =====================================================================
function loadUserName() { /* replaced by initUserSystem */ }
function saveUserName() { showToast('请在"切换"按钮里修改用户', 'info'); }

function updateTimeGreeting() {
  const info = getGreetingInfo();
  if (dom.timeGreeting) dom.timeGreeting.textContent = info.text;
  const iconBox = document.getElementById('timeGreetingIcon');
  if (iconBox) {
    iconBox.innerHTML = `<i data-lucide="${info.icon}" style="width:24px;height:24px;"></i>`;
    if (window.lucide) window.lucide.createIcons();
  }
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
  const idle = document.getElementById('voiceIdleState');
  const rec  = document.getElementById('voiceRecordingState');
  if (idle) idle.classList.remove('hidden');
  if (rec)  rec.classList.add('hidden');
  stopMicLipSync();
}

function startVoiceRecording() {
  state.pressTimer = setTimeout(() => {
    state.isLongPress = true;
    const btn  = document.getElementById('centerVoiceBtn');
    const idle = document.getElementById('voiceIdleState');
    const rec  = document.getElementById('voiceRecordingState');
    if (btn)  btn.classList.add('recording');
    if (idle) idle.classList.add('hidden');
    if (rec)  { rec.classList.remove('hidden'); rec.classList.add('flex'); }
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
    stopVoiceUI();
    if (state.recognition) { try { state.recognition.stop(); } catch (_) { stopVoiceUI(); } }
  } else {
    showToast('💡 长按按钮可以语音输入哦', 'info');
  }
}

// ---------------------- Page Navigation ----------------------
async function selectAvatar(id) {
  if (!state.userId) {
    showUserModal();
    showToast('请先选择或创建您的用户', 'warning');
    return;
  }
  state.avatar = AVATARS[id];
  dom.botName.textContent = state.avatar.name;
  dom.botDesc.textContent = state.avatar.desc;
  dom.avatarContainer.className = `avatar-image ${state.avatar.skinClass}`;
  dom.avatarContainer.textContent = state.avatar.icon;
  // 把 bot 消息气泡左侧的小头像换成对应角色（CSS 变量驱动 .message-bot::before）
  if (state.avatar.imagePath) {
    document.documentElement.style.setProperty('--bot-avatar-url', `url('${state.avatar.imagePath}')`);
  }
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
  // 选头像页已建立连接时直接发 init，否则新建连接（onopen 会发）
  if (state.wsConnected) {
    wsSend({ type: 'init', avatarId: state.avatar.id, avatarName: state.avatar.name, sessionId: state.sessionId, userName: state.userName, userId: state.userId });
  } else {
    connectWebSocket();
  }
  Camera.init();
  dom.messageInput.focus();
  setStatus(STATUS.online);
}

function goBack() {
  // 先清头像，再关闭连接；onclose 重连时 state.avatar===null 不会发 init
  state.avatar = null;
  state.reconnects = 0; // 允许在选头像页继续保活
  if (state.ws) state.ws.close();
  AudioPlayer.reset();
  resetBotBubbleState();
  Camera.stop();
  dom.chatPage.classList.remove('active');
  dom.selectPage.classList.add('active');
  destroyAvatar();
  dom.messagesContainer.innerHTML = '';
}

// ---------------------- Init ----------------------
window.addEventListener('DOMContentLoaded', () => {
  cacheDom();
  applyCareModePreference();
  initUserSystem();
  updateTimeGreeting();
  setInterval(updateTimeGreeting, 60000);
  initVoiceRecognition();
  initSessionListEvents();
  connectWebSocket(); // 页面加载即建立连接，选头像页同样保活

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
// 用户系统
window.showUserModal = showUserModal;
window.hideUserModal = hideUserModal;
window.selectUser = selectUser;
window.confirmNewUser = confirmNewUser;
